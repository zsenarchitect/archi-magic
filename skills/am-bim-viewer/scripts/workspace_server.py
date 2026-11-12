"""Serve authenticated office workspace review state behind an HTTPS proxy."""

import argparse
import asyncio
from collections import deque
from contextlib import ExitStack
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
from time import monotonic
from urllib.parse import urlsplit

from aiohttp import web

from workspace_review_state import OperationConflict, ReviewState, RevisionConflict


COOKIE = "__Host-archi-workspace"
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 200
MAX_LOGIN_ATTEMPTS = 60
MAX_WORKERS = 4
MAX_BODY_BYTES = 32 * 1024
LOG = logging.getLogger(__name__)


def canonical_origin(origin):
    parts = urlsplit(origin)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
            or origin != "https://" + parts.netloc or parts.netloc != parts.netloc.lower()
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[1-9][0-9]{0,4})?", parts.netloc)):
        raise ValueError("Configure one canonical HTTPS origin, with no path or credentials.")
    if parts.port is not None and parts.port == 443:
        raise ValueError("Omit the default HTTPS port from the canonical origin.")
    return parts.netloc


class Workspace:
    def __init__(self, origin, accounts, projects):
        self.authority = canonical_origin(origin)
        self.origin = origin
        if (not isinstance(accounts, dict) or not accounts
                or any(not isinstance(actor, str) or not actor.strip() or not isinstance(digest, str)
                       or re.fullmatch(r"[0-9a-f]{64}", digest) is None for actor, digest in accounts.items())
                or len(set(accounts.values())) != len(accounts)):
            raise ValueError("Configure a unique token digest per account.")
        if not isinstance(projects, dict) or not projects:
            raise ValueError("Configure at least one project.")
        self.account_by_digest = {digest: actor for actor, digest in accounts.items()}
        self.projects = {}
        roots = []
        for slug, config in projects.items():
            path = Path(config["root"])
            root = path.resolve(strict=True)
            if (not path.is_absolute() or root != path or not root.is_dir()
                    or any(root == other or root in other.parents or other in root.parents for other in roots)):
                raise ValueError("Configure disjoint physical project roots without symbolic links.")
            if set(config["members"]) - set(accounts):
                raise ValueError("Project membership requires a configured account.")
            roots.append(root)
            self.projects[slug] = ReviewState(root, slug, config["members"], config["targets"])
        self.sessions = {}
        self.login_attempts = deque()
        self.workers = set()

    async def lifecycle(self, _app):
        with ExitStack() as states:
            for project in self.projects.values():
                states.enter_context(project)
            try:
                yield
            finally:
                await asyncio.gather(*self.workers, return_exceptions=True)

    async def storage(self, function, *args):
        if len(self.workers) >= MAX_WORKERS:
            raise web.HTTPServiceUnavailable(text="The workspace is busy. Retry shortly.")
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        self.workers.add(worker)

        def finished(task):
            self.workers.discard(task)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(finished)
        return await asyncio.shield(worker)

    def session_actor(self, request):
        token = request.cookies.get(COOKIE)
        session = self.sessions.get(token)
        if session is None or session[1] <= monotonic():
            self.sessions.pop(token, None)
            raise web.HTTPUnauthorized(text="Sign in to the office workspace.")
        return session[0]

    def project_member(self, request, edit=False):
        actor = self.session_actor(request)
        project = self.projects.get(request.match_info["project"])
        if project is None or actor not in project.members:
            raise web.HTTPNotFound(text="Project unavailable.")
        if edit and project.members[actor] != "editor":
            raise web.HTTPForbidden(text="This project requires an editor.")
        return actor, project

    async def login(self, request):
        now = monotonic()
        while self.login_attempts and self.login_attempts[0] <= now - 60:
            self.login_attempts.popleft()
        if len(self.login_attempts) >= MAX_LOGIN_ATTEMPTS:
            raise web.HTTPTooManyRequests(text="Try signing in again in a minute.")
        self.login_attempts.append(now)
        body = await read_json(request)
        token = body.get("token")
        if set(body) != {"token"} or not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
            raise web.HTTPUnauthorized(text="Invalid workspace credentials.")
        actor = self.account_by_digest.get(hashlib.sha256(token.encode("ascii")).hexdigest())
        if actor is None:
            raise web.HTTPUnauthorized(text="Invalid workspace credentials.")
        self.sessions = {key: session for key, session in self.sessions.items() if session[1] > now}
        self.sessions.pop(request.cookies.get(COOKIE), None)
        if len(self.sessions) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(text="The workspace session limit was reached.")
        session_token = secrets.token_urlsafe(32)
        self.sessions[session_token] = (actor, now + SESSION_SECONDS)
        response = web.json_response({"actor": actor})
        response.set_cookie(COOKIE, session_token, secure=True, httponly=True, samesite="Strict", max_age=SESSION_SECONDS)
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE), None)
        response = web.Response(status=204)
        response.set_cookie(COOKIE, "", secure=True, httponly=True, samesite="Strict", max_age=0)
        return response

    async def read_review(self, request):
        actor, project = self.project_member(request)
        try:
            result = await self.storage(project.read, actor)
        except (OSError, ValueError) as error:
            LOG.warning("Workspace state could not be read (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Project review state is unavailable.") from error
        return web.json_response({**result, "role": project.members[actor], "members": sorted(project.members)},
                                 headers={"ETag": result["etag"]})

    async def update_review(self, request):
        actor, project = self.project_member(request, edit=True)
        tags = request.headers.getall("If-Match", [])
        if not tags:
            raise web.HTTPPreconditionRequired(text="Refresh to get the project revision before editing.")
        if len(tags) != 1 or re.fullmatch(r'"[a-z0-9][a-z0-9-]{0,62}-[0-9]+"', tags[0]) is None:
            raise web.HTTPBadRequest(text="Use one strong project revision in If-Match.")
        body = await read_json(request)
        if set(body) != {"target", "operation_id", "patch"}:
            raise web.HTTPBadRequest(text="Supply a target, operation ID and annotation change.")
        try:
            result = await self.storage(project.update, actor, body["target"], body["operation_id"], tags[0], body["patch"])
        except RevisionConflict as error:
            raise web.HTTPPreconditionFailed(text=str(error)) from error
        except OperationConflict as error:
            raise web.HTTPConflict(text=str(error)) from error
        except (OSError, json.JSONDecodeError) as error:
            LOG.warning("Workspace state could not be saved (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Project review state is unavailable.") from error
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        return web.json_response(result, headers={"ETag": result["etag"]})


async def read_json(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Use application/json.")
    try:
        body = await request.json()
    except (ValueError, UnicodeError, RecursionError) as error:
        raise web.HTTPBadRequest(text="Use a valid JSON object.") from error
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Use a valid JSON object.")
    return body


def create_app(origin, accounts, projects):
    workspace = Workspace(origin, accounts, projects)
    security_headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.headers.getall("Host", []) != [workspace.authority]:
                raise web.HTTPBadRequest(text="Unexpected workspace host.")
            if request.method not in ("GET", "HEAD") and request.headers.getall("Origin", []) != [workspace.origin]:
                raise web.HTTPForbidden(text="Unexpected workspace origin.")
            response = await handler(request)
        except web.HTTPException as error:
            error.headers.update(security_headers)
            raise
        response.headers.update(security_headers)
        return response

    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[boundary])
    app.cleanup_ctx.append(workspace.lifecycle)
    app.add_routes([
        web.post("/session", workspace.login),
        web.delete("/session", workspace.logout),
        web.get("/projects/{project}/review", workspace.read_review),
        web.patch("/projects/{project}/review", workspace.update_review),
    ])
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Private JSON configuration outside served directories")
    parser.add_argument("--port", type=int, default=8765, help="Local port for the HTTPS proxy upstream")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        app = create_app(config["origin"], config["accounts"], config["projects"])
    except (KeyError, TypeError, ValueError, OSError):
        parser.error("Invalid office workspace configuration.")
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
