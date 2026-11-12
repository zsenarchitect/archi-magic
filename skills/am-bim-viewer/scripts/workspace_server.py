"""Serve authenticated office workspace review state behind an HTTPS proxy."""

import argparse
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import subprocess
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from aiohttp import WSCloseCode, WSMsgType, web

from workspace_artifacts import (
    ARTIFACT_KINDS, board_binding, open_json_file, read_json_stream, read_published, review_targets,
    slot_directory, slot_limit, slot_path, validate_publication,
)
from workspace_audit_report import AuditReportTooLarge, build_audit_report
from workspace_documents import copy_document
from workspace_model import ModelViewer, VIEWER_POLICY, build_model
from workspace_page import describe_artifact, page
from workspace_review_state import OperationConflict, ReviewState, RevisionConflict


COOKIE = "__Host-archi-workspace"
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 200
MAX_LOGIN_ATTEMPTS = 60
MAX_WORKERS = 4
MAX_BODY_BYTES = 32 * 1024
MAX_SOCKETS = 50
MAX_PROJECT_SOCKETS = 10
MAX_SOCKET_BYTES = 1024
SOCKET_HEARTBEAT = 20
SOCKET_WRITE_TIMEOUT = 2
ARTIFACT_POLL_SECONDS = 2
SECURITY_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}
LOG = logging.getLogger(__name__)
MODEL_RESPONSE = web.ResponseKey("model_viewer", ModelViewer)


def canonical_origin(origin):
    parts = urlsplit(origin)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
            or origin != "https://" + parts.netloc or parts.netloc != parts.netloc.lower()
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[1-9][0-9]{0,4})?", parts.netloc)):
        raise ValueError("Configure one canonical HTTPS origin, with no path or credentials.")
    if parts.port is not None and parts.port == 443:
        raise ValueError("Omit the default HTTPS port from the canonical origin.")
    return parts.netloc


@dataclass
class Subscription:
    project: str
    actor: str
    session: str
    pending: asyncio.Queue

    def replace_pending(self, message):
        if self.pending.full():
            self.pending.get_nowait()
        self.pending.put_nowait(message)


class Workspace:
    def __init__(self, origin, accounts, projects):
        self.authority = canonical_origin(origin)
        self.origin = origin
        self.page_html, self.page_policy = page(origin)
        if (not isinstance(accounts, dict) or not accounts
                or any(not isinstance(actor, str) or not actor.strip() or not isinstance(digest, str)
                       or re.fullmatch(r"[0-9a-f]{64}", digest) is None for actor, digest in accounts.items())
                or len(set(accounts.values())) != len(accounts)):
            raise ValueError("Configure a unique token digest per account.")
        if not isinstance(projects, dict) or not projects:
            raise ValueError("Configure at least one project.")
        self.account_by_digest = {digest: actor for actor, digest in accounts.items()}
        self.projects = {}
        self.artifact_slots = {}
        self.document_slots = {}
        self.audit_slots = {}
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
            self.projects[slug] = ReviewState(root, slug, config["members"], config.get("targets", []))
            slots = config.get("artifacts", {})
            if not isinstance(slots, dict) or any(
                    not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) is None
                    or not isinstance(slot, dict) or slot.get("kind") not in ARTIFACT_KINDS
                    for name, slot in slots.items()):
                raise ValueError("Configure named audit, preview, board, model or document artifact slots.")
            paths = [slot_path(slot) for slot in slots.values()]
            if len(set(paths)) != len(paths):
                raise ValueError("Each artifact slot requires a different file.")
            lineages = [board_binding(slot)["lineage"] for slot in slots.values() if slot["kind"] == "board"]
            if len(set(lineages)) != len(lineages):
                raise ValueError("Each board slot requires a separate lineage.")
            namespaces = tuple([f"board:{lineage}:" for lineage in lineages] + [
                f"{slot['kind']}:{name}:" for name, slot in slots.items() if slot["kind"] != "board"])
            if any(not isinstance(target, str) or target.startswith(namespaces) for target in self.projects[slug].targets):
                raise ValueError("Explicit targets cannot overlap a published artifact's target namespace.")
            self.artifact_slots[slug] = slots
            documents = {name: slot for name, slot in slots.items() if slot["kind"] == "document"}
            if len(documents) > 4 or sum(map(len, self.document_slots.values())) + len(documents) > 32:
                raise ValueError("Configure at most four document slots per project and 32 per service.")
            self.document_slots[slug] = documents
            audit_reports = config.get("audit_reports", True)
            if type(audit_reports) is not bool:
                raise ValueError("Set audit_reports to true or false.")
            self.audit_slots[slug] = tuple(name for name, slot in slots.items()
                                           if audit_reports and slot["kind"] == "audit")
        self.model_build_lock = Lock()
        self.model_publications = {}
        self.sessions = {}
        self.login_attempts = deque()
        self.workers = set()
        self.document_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-document")
        self.document_worker = None
        self.document_project = None
        self.pending_documents = None
        self.documents_stopping = False
        self.document_request = False
        self.document_publications = {}
        self.audit_request = False
        self.audit_worker = None
        self.sockets = {}
        self.last_notified = {}
        self.watchers = {}

    async def lifecycle(self, _app):
        with ExitStack() as states:
            for project in self.projects.values():
                states.enter_context(project)
            try:
                yield
            finally:
                await asyncio.gather(*self.workers, return_exceptions=True)
                if self.audit_worker is not None:
                    await asyncio.gather(self.audit_worker, return_exceptions=True)
                if self.document_worker is not None:
                    await asyncio.gather(self.document_worker, return_exceptions=True)
                self.document_executor.shutdown()
                self.document_publications.clear()
                self.model_publications.clear()

    async def storage(self, function, *args, on_success=None):
        if len(self.workers) >= MAX_WORKERS:
            raise web.HTTPServiceUnavailable(text="The workspace is busy. Retry shortly.")
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        self.workers.add(worker)

        def finished(task):
            self.workers.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is None and on_success is not None:
                    on_success(task.result())

        worker.add_done_callback(finished)
        return await asyncio.shield(worker)

    def session_actor(self, request):
        token = request.cookies.get(COOKIE)
        session = self.sessions.get(token)
        if session is None or session[1] <= monotonic():
            self.sessions.pop(token, None)
            raise web.HTTPUnauthorized(text="Sign in to the office workspace.")
        return session[0]

    def revoke_session(self, token):
        if self.sessions.pop(token, None) is not None:
            for subscriber in self.sockets.values():
                if subscriber.session == token:
                    subscriber.replace_pending(None)

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
        self.revoke_session(request.cookies.get(COOKIE))
        if len(self.sessions) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(text="The workspace session limit was reached.")
        session_token = secrets.token_urlsafe(32)
        self.sessions[session_token] = (actor, now + SESSION_SECONDS)
        response = web.json_response({"actor": actor})
        response.set_cookie(COOKIE, session_token, secure=True, httponly=True, samesite="Strict", max_age=SESSION_SECONDS)
        return response

    async def logout(self, request):
        self.revoke_session(request.cookies.get(COOKIE))
        response = web.Response(status=204)
        response.set_cookie(COOKIE, "", secure=True, httponly=True, samesite="Strict", max_age=0)
        return response

    async def sign_in_page(self, request):
        return web.Response(text=self.page_html, content_type="text/html",
                            headers={"Content-Security-Policy": self.page_policy})

    async def project_page(self, request):
        try:
            self.project_member(request)
        except web.HTTPUnauthorized:
            slug = request.match_info["project"]
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug) is None:
                raise web.HTTPNotFound(text="Project unavailable.")
            raise web.HTTPFound(location=f"/?project={slug}")
        return await self.sign_in_page(request)

    def workspace_snapshot(self, actor, project):
        review, artifacts = project.read_workspace(actor)
        slots = self.artifact_slots[project.project]
        return {**review, "role": project.members[actor], "members": sorted(project.members),
                "audit_report_slots": self.audit_slots[project.project],
                "artifacts": {name: describe_artifact(artifacts.get(name, {"freshness": "unavailable"}))
                              for name in slots}}

    async def read_workspace(self, request):
        actor, project = self.project_member(request)
        self.schedule_document_refresh(project)
        try:
            await self.refresh_artifacts(project)
            result = await self.storage(self.workspace_snapshot, actor, project)
        except (OSError, ValueError) as error:
            LOG.warning("Workspace snapshot could not be read (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Project workspace is unavailable.") from error
        return web.json_response(result, headers={"ETag": result["etag"]})

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
            await self.refresh_artifacts(project)
            result, _ = await self.storage(project.update_with_outcome, actor, body["target"],
                                           body["operation_id"], tags[0], body["patch"],
                                           on_success=lambda outcome: self.notify_review(project.project, outcome))
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

    def notify_review(self, project, outcome):
        receipt, committed = outcome
        if committed:
            self.notify(project, "review", receipt["etag"])

    def notify(self, project, kind, etag):
        if etag is None:
            return
        revision = int(etag[:-1].rsplit("-", 1)[1])
        if revision <= self.last_notified.get(project, 0):
            return
        self.last_notified[project] = revision
        for subscriber in self.sockets.values():
            if subscriber.project == project:
                subscriber.replace_pending({"type": kind, "etag": etag})

    def load_model(self, root, name, slot):
        key = (root, name)
        limit = slot_limit(slot)
        with slot_directory(root, slot) as (directory, filename):
            with open_json_file(filename, limit, directory) as (stream, metadata):
                signature = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
                             metadata.st_ctime_ns)
                prior = self.model_publications.get(key)
                if prior is not None and prior[0] == signature:
                    return prior[1]
                with self.model_build_lock:
                    publication = validate_publication(read_json_stream(stream, limit), slot)
                    if prior is not None and publication["digest"] == prior[1]["digest"]:
                        viewer = prior[1]["data"]
                    else:
                        viewer = build_model(publication["data"])
                    publication["data"] = viewer
                self.model_publications[key] = (signature, publication)
                return publication

    def load_artifact(self, root, name, slot):
        try:
            artifact = self.load_model(root, name, slot) if slot["kind"] == "model" else read_published(root, slot)
            return artifact, review_targets(name, artifact)
        except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError, subprocess.SubprocessError) as error:
            if slot["kind"] == "model" and not isinstance(error, FileNotFoundError):
                LOG.warning("Workspace model publication could not be loaded (%s)", type(error).__name__)
            return None, []

    def load_document(self, project, name, slot):
        key = (project.project, name)
        limit = slot_limit(slot)
        signature = None
        try:
            with slot_directory(project.root, slot) as (directory, filename):
                with open_json_file(filename, limit, directory) as (stream, metadata):
                    signature = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
                                 metadata.st_ctime_ns)
                    prior = self.document_publications.get(key)
                    if prior is not None and prior[0] == signature:
                        return prior[1]
                    publication = validate_publication(read_json_stream(stream, limit), slot)
            if not project.accepts_document(name, publication):
                publication = None
            elif prior is not None and prior[1] is not None and publication["digest"] == prior[1]["digest"]:
                publication["data"] = prior[1]["data"]
            else:
                publication["data"] = copy_document(publication["data"])
        except (OSError, RecursionError, ValueError) as error:
            if not isinstance(error, FileNotFoundError):
                LOG.warning("Workspace document publication could not be loaded (%s)", type(error).__name__)
            publication = None
        if signature is not None:
            self.document_publications[key] = (signature, publication)
        else:
            self.document_publications.pop(key, None)
        return publication

    def refresh_documents(self, project, slots):
        latest = None
        for name, slot in slots.items():
            publication = self.load_document(project, name, slot)
            revision = project.refresh_artifacts({name: slot}, lambda *_: (publication, []))
            if revision is not None:
                latest = revision
        return latest

    def start_document_worker(self, project, function, on_success):
        self.document_project = project
        worker = self.document_worker = asyncio.get_running_loop().run_in_executor(self.document_executor, function)

        def finished(task):
            self.document_worker = None
            self.document_project = None
            if not task.cancelled():
                error = task.exception()
                if error is None:
                    self.notify(project.project, "artifacts", on_success(task.result()))
                elif isinstance(error, (OSError, ValueError)):
                    LOG.warning("Workspace document operation failed (%s)", type(error).__name__)
            self.start_pending_documents()

        worker.add_done_callback(finished)
        return worker

    def schedule_document_refresh(self, project):
        slots = self.document_slots[project.project]
        if not slots or self.documents_stopping:
            return
        if self.document_worker is not None or self.document_request:
            if project is not self.document_project and self.pending_documents is None:
                self.pending_documents = project
            return
        self.start_document_worker(project, lambda: self.refresh_documents(project, slots), lambda revision: revision)

    def start_pending_documents(self):
        if self.pending_documents is not None and self.document_worker is None and not self.document_request:
            project, self.pending_documents = self.pending_documents, None
            self.schedule_document_refresh(project)

    async def refresh_artifacts(self, project):
        slots = {name: slot for name, slot in self.artifact_slots[project.project].items()
                 if slot["kind"] != "document"}
        if slots:
            await self.storage(project.refresh_artifacts, slots, self.load_artifact,
                               on_success=lambda etag: self.notify(project.project, "artifacts", etag))

    async def watch_artifacts(self, project):
        while True:
            self.schedule_document_refresh(project)
            try:
                await self.refresh_artifacts(project)
            except web.HTTPServiceUnavailable:
                pass  # The worker pool is busy. The next poll retries.
            except (OSError, ValueError) as error:
                LOG.warning("Workspace artifacts could not be refreshed (%s)", type(error).__name__)
            await asyncio.sleep(ARTIFACT_POLL_SECONDS)

    async def read_artifact(self, request):
        actor, project = self.project_member(request)
        slot = request.match_info["slot"]
        if slot not in self.artifact_slots[project.project]:
            raise web.HTTPNotFound(text="Artifact unavailable.")
        if slot in self.document_slots[project.project]:
            self.schedule_document_refresh(project)
        try:
            await self.refresh_artifacts(project)
            data = await self.storage(project.read_artifact, actor, slot)
        except (OSError, ValueError) as error:
            LOG.warning("Workspace artifact could not be read (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Project artifact is unavailable.") from error
        if data.get("kind") in ("model", "document"):
            data = describe_artifact(data)
        return web.json_response(data, status=503 if data["freshness"] == "unavailable" else 200)

    async def read_document(self, request):
        actor, project = self.project_member(request)
        name = request.match_info["slot"]
        slot = self.document_slots[project.project].get(name)
        if slot is None:
            raise web.HTTPNotFound(text="Document unavailable.")
        collection = request.match_info["collection"]
        query = request.query
        if (set(query) not in (set(), {"digest", "generation", "index"})
                or any(len(query.getall(key)) != 1 for key in query)):
            raise web.HTTPBadRequest(text="Use the complete document page cursor.")
        digest = query.get("digest")
        generation = query.get("generation", "0")
        index = query.get("index", "0")
        if ((digest is not None and re.fullmatch(r"[0-9a-f]{64}", digest) is None)
                or any(re.fullmatch(r"(0|[1-9][0-9]{0,15})", value) is None for value in (generation, index))):
            raise web.HTTPBadRequest(text="Use a valid document page cursor.")
        if self.document_request or self.document_worker is not None:
            raise web.HTTPServiceUnavailable(text="The document worker is busy. Retry shortly.",
                                             headers={"Retry-After": "2"})

        def render():
            revision = self.refresh_documents(project, {name: slot})
            entry = project.read_artifact(actor, name)
            if entry["freshness"] == "unavailable":
                return revision, 503, None
            current = entry["digest"], entry["generation"]
            if digest is not None and current != (digest, int(generation)):
                return revision, 409, None
            try:
                body = entry["data"].read_page(collection, int(index), entry["generation"],
                                               entry["digest"], entry["freshness"])
            except LookupError:
                return revision, 404, None
            except ValueError:
                return revision, 400, None
            return revision, 200, body

        self.document_request = True
        worker = self.start_document_worker(project, render, lambda result: result[0])
        try:
            try:
                _, status, body = await asyncio.shield(worker)
            except (OSError, ValueError) as error:
                raise web.HTTPServiceUnavailable(text="Document publication unavailable.") from error
            if status == 409:
                raise web.HTTPConflict(text="The document changed. Open the current publication.")
            if status == 404:
                raise web.HTTPNotFound(text="Document collection unavailable.")
            if status == 400:
                raise web.HTTPBadRequest(text="The document page index is outside the collection.")
            if status == 503:
                raise web.HTTPServiceUnavailable(text="Document publication unavailable.")
            response = web.StreamResponse(headers={**SECURITY_HEADERS, "Content-Type": "application/json",
                                                   "Content-Length": str(len(body))})
            await response.prepare(request)
            if request.method != "HEAD":
                for offset in range(0, len(body), 128 * 1024):
                    await response.write(body[offset:offset + 128 * 1024])
            await response.write_eof()
            return response
        finally:
            if worker.done():
                self.release_document_request(worker)
            else:
                worker.add_done_callback(self.release_document_request)

    def release_document_request(self, _worker):
        self.document_request = False
        self.start_pending_documents()

    async def read_audit_report(self, request):
        actor, project = self.project_member(request)
        allowed = self.audit_slots[project.project]
        selected = request.query.getall("slot", allowed)
        if not allowed or not selected or any(slot not in allowed for slot in selected):
            raise web.HTTPNotFound(text="Audit report unavailable.")
        if set(request.query) - {"slot"} or len(selected) != len(set(selected)):
            raise web.HTTPBadRequest(text="Select each audit slot at most once.")
        if self.audit_request:
            raise web.HTTPServiceUnavailable(text="An audit report is being generated. Retry shortly.",
                                             headers={"Retry-After": "2"})
        self.audit_request = True
        worker = None

        def release(task=None):
            if task is not None and not task.cancelled():
                task.exception()
            self.audit_worker = None
            self.audit_request = False

        def render():
            revision, entries = project.read_audits(actor, selected)
            return build_audit_report(project.project, revision, entries)

        try:
            worker = self.audit_worker = asyncio.create_task(asyncio.to_thread(render))
            try:
                body = await asyncio.shield(worker)
            except AuditReportTooLarge as error:
                raise web.HTTPRequestEntityTooLarge(max_size=0, actual_size=1, text=str(error)) from error
            except (OSError, ValueError) as error:
                LOG.warning("Workspace audit report could not be rendered (%s)", type(error).__name__)
                raise web.HTTPServiceUnavailable(text="Project audit report is unavailable.") from error
            headers = dict(SECURITY_HEADERS, **{
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(body)),
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            })
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            if request.method != "HEAD":
                for offset in range(0, len(body), 128 * 1024):
                    await response.write(body[offset:offset + 128 * 1024])
            await response.write_eof()
            return response
        finally:
            if worker is not None and not worker.done():
                worker.add_done_callback(release)
            else:
                release()

    async def read_model(self, request):
        actor, project = self.project_member(request)
        slot = request.match_info["slot"]
        config = self.artifact_slots[project.project].get(slot)
        if config is None or config["kind"] != "model":
            raise web.HTTPNotFound(text="Model unavailable.")
        try:
            await self.refresh_artifacts(project)
            artifact = await self.storage(project.read_artifact, actor, slot)
        except (OSError, ValueError) as error:
            LOG.warning("Workspace model could not be read (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Project model is unavailable.") from error
        viewer = artifact.get("data")
        if viewer is None or viewer.digest != request.match_info["digest"]:
            raise web.HTTPNotFound(text="Model unavailable.")
        response = web.FileResponse(viewer.path, headers={"Content-Type": "text/html; charset=utf-8",
                                                           "Content-Security-Policy": VIEWER_POLICY,
                                                           "Referrer-Policy": "no-referrer"})
        # FileResponse opens the file after the handler returns. Retain the private build through streaming.
        response[MODEL_RESPONSE] = viewer
        return response

    def subscription_lifetime(self, subscriber):
        session = self.sessions.get(subscriber.session)
        if (session is None or session[0] != subscriber.actor
                or subscriber.actor not in self.projects[subscriber.project].members):
            return 0
        return session[1] - monotonic()

    async def send_updates(self, ws, subscriber):
        while self.subscription_lifetime(subscriber) > 0:
            timeout = min(SOCKET_HEARTBEAT, self.subscription_lifetime(subscriber))
            try:
                message = await asyncio.wait_for(subscriber.pending.get(), timeout)
            except asyncio.TimeoutError:
                continue
            if message is None or self.subscription_lifetime(subscriber) <= 0:
                break
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True)
            if len(payload) > MAX_SOCKET_BYTES:
                break
            try:
                await asyncio.wait_for(ws.send_str(payload), SOCKET_WRITE_TIMEOUT)
            except (asyncio.TimeoutError, ConnectionError):
                break
        await ws.close(code=WSCloseCode.POLICY_VIOLATION)

    async def subscribe(self, request):
        if request.headers.getall("Origin", []) != [self.origin]:
            raise web.HTTPForbidden(text="Unexpected workspace origin.")
        actor, project = self.project_member(request)
        if (len(self.sockets) >= MAX_SOCKETS
                or sum(item.project == project.project for item in self.sockets.values()) >= MAX_PROJECT_SOCKETS):
            raise web.HTTPServiceUnavailable(text="The workspace connection limit was reached.")
        ws = web.WebSocketResponse(heartbeat=SOCKET_HEARTBEAT, timeout=SOCKET_WRITE_TIMEOUT,
                                   compress=False, max_msg_size=MAX_SOCKET_BYTES, writer_limit=MAX_SOCKET_BYTES)
        ws.headers.update(SECURITY_HEADERS)
        subscriber = Subscription(project.project, actor, request.cookies[COOKIE], asyncio.Queue(maxsize=1))
        self.sockets[ws] = subscriber
        sender = None
        try:
            await ws.prepare(request)
            if self.artifact_slots[project.project] and project.project not in self.watchers:
                self.watchers[project.project] = asyncio.create_task(self.watch_artifacts(project))
            sender = asyncio.create_task(self.send_updates(ws, subscriber))
            async for message in ws:
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    await ws.close(code=WSCloseCode.POLICY_VIOLATION)
                    break
        finally:
            self.sockets.pop(ws, None)
            if not any(item.project == project.project for item in self.sockets.values()):
                watcher = self.watchers.pop(project.project, None)
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def shutdown(self, _app):
        self.documents_stopping = True
        self.pending_documents = None
        watchers = tuple(self.watchers.values())
        self.watchers.clear()
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        await asyncio.gather(*(ws.close(code=WSCloseCode.GOING_AWAY) for ws in tuple(self.sockets)),
                             return_exceptions=True)


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

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.headers.getall("Host", []) != [workspace.authority]:
                raise web.HTTPBadRequest(text="Unexpected workspace host.")
            if request.method not in ("GET", "HEAD") and request.headers.getall("Origin", []) != [workspace.origin]:
                raise web.HTTPForbidden(text="Unexpected workspace origin.")
            response = await handler(request)
        except web.HTTPException as error:
            error.headers.update(SECURITY_HEADERS)
            raise
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[boundary])
    app.cleanup_ctx.append(workspace.lifecycle)
    app.on_shutdown.append(workspace.shutdown)
    app.add_routes([
        web.get("/", workspace.sign_in_page),
        web.get("/projects/{project}", workspace.project_page),
        web.get("/projects/{project}/workspace", workspace.read_workspace),
        web.post("/session", workspace.login),
        web.delete("/session", workspace.logout),
        web.get("/projects/{project}/review", workspace.read_review),
        web.patch("/projects/{project}/review", workspace.update_review),
        web.get("/projects/{project}/events", workspace.subscribe, allow_head=False),
        web.get("/projects/{project}/artifacts/{slot}", workspace.read_artifact),
        web.get("/projects/{project}/audit-report", workspace.read_audit_report),
        web.get("/projects/{project}/documents/{slot}/{collection}", workspace.read_document),
        web.get(r"/projects/{project}/models/{slot}/{digest:[0-9a-f]{64}}.html", workspace.read_model),
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
