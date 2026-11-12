import asyncio
import hashlib
import os
from pathlib import Path
import sys
from threading import Event

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace_server as server
from workspace_artifacts import publish
from workspace_server import COOKIE, create_app


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Office state locking requires POSIX.")
ORIGIN = "https://review.example.test"
HEADERS = {"Host": "review.example.test", "Origin": ORIGIN}
TOKENS = {"editor": "e" * 32, "viewer": "v" * 32, "outsider": "o" * 32}
TARGET = "board:sample:1"
REVIEW = "/projects/sample/review"


def settings(tmp_path):
    root = tmp_path / "sample"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    accounts = {actor: hashlib.sha256(token.encode()).hexdigest() for actor, token in TOKENS.items()}
    projects = {"sample": {"root": root.resolve(), "members": {"editor": "editor", "viewer": "viewer"}, "targets": [TARGET]},
                "other": {"root": other.resolve(), "members": {"outsider": "editor"}, "targets": [TARGET]}}
    return accounts, projects


def test_http_requires_project_membership_and_exact_revisions(tmp_path):
    accounts, projects = settings(tmp_path)

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                assert response.status == 200
                assert await response.json() == {"actor": actor}
                cookie = response.cookies[COOKIE]
                assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict"
                assert cookie["path"] == "/" and not cookie["domain"]
                return {COOKIE: cookie.value}

            anonymous = await client.get(REVIEW, headers=HEADERS)
            assert anonymous.status == 401
            editor, viewer, outsider = [await login(actor) for actor in ("editor", "viewer", "outsider")]
            assert (await client.get(REVIEW, headers=HEADERS, cookies=outsider)).status == 404
            assert (await client.head(REVIEW, headers=HEADERS, cookies=outsider)).status == 404
            assert (await client.get("/projects/other/review", headers=HEADERS, cookies=editor)).status == 404
            response = await client.get(REVIEW, headers=HEADERS, cookies=viewer)
            initial = response.headers["ETag"]
            assert await response.json() == {"etag": initial, "annotations": {}, "role": "viewer", "members": ["editor", "viewer"]}
            assert response.headers["Cache-Control"] == "private, no-store"

            body = {"target": TARGET, "operation_id": "operation-0000001", "patch": {"comment": "Check the sill."}}
            headers = dict(HEADERS, **{"If-Match": initial})
            assert (await client.patch(REVIEW, headers=headers, cookies=viewer, json=body)).status == 403
            assert (await client.patch(REVIEW, headers=HEADERS, cookies=editor, json=body)).status == 428
            assert (await client.patch(REVIEW, headers=dict(headers, **{"If-Match": "*"}), cookies=editor, json=body)).status == 400
            update = await client.patch(REVIEW, headers=headers, cookies=editor, json=body)
            assert update.status == 200
            assert update.headers["ETag"] != initial
            receipt = await update.json()
            replay = await client.patch(REVIEW, headers=headers, cookies=editor, json=body)
            assert replay.status == 200 and await replay.json() == receipt
            assert (await client.patch(REVIEW, headers=headers, cookies=editor,
                                       json=dict(body, operation_id="operation-0000002"))).status == 412
            assert (await client.patch(REVIEW, headers=headers, cookies=editor,
                                       json=dict(body, patch={"comment": "Other"}))).status == 409
            response = await client.get(REVIEW, headers=HEADERS, cookies=viewer)
            notes = (await response.json())["annotations"][TARGET]["comments"]
            assert [(note["actor"], note["text"]) for note in notes] == [("editor", "Check the sill.")]
            assert (await client.delete("/session", headers=HEADERS, cookies=editor)).status == 204
            assert (await client.get(REVIEW, headers=HEADERS, cookies=editor)).status == 401

    asyncio.run(exercise())


def test_http_rejects_forged_origin_and_host_and_does_not_serve_sidecars(tmp_path):
    accounts, projects = settings(tmp_path)

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            forged_host = dict(HEADERS, Host="wrong.example.test", **{"X-Forwarded-Host": HEADERS["Host"]})
            login = await client.post("/session", headers=forged_host, json={"token": TOKENS["editor"]})
            assert login.status == 400
            forged_origin = dict(HEADERS, Origin="https://wrong.example.test")
            login = await client.post("/session", headers=forged_origin, json={"token": TOKENS["editor"]})
            assert login.status == 403
            login = await client.post("/session", headers=HEADERS, json={"token": "x" * (32 * 1024)})
            assert login.status == 413
            login = await client.post("/session", headers=HEADERS, json={"token": TOKENS["editor"]})
            cookie = {COOKIE: login.cookies[COOKIE].value}
            assert (await client.get("/projects/sample/.am-issues.json", headers=HEADERS, cookies=cookie)).status == 404
            assert (await client.delete("/session", headers=forged_origin, cookies=cookie)).status == 403

    asyncio.run(exercise())


def test_workspace_rejects_nested_or_aliased_project_roots(tmp_path):
    accounts, projects = settings(tmp_path)
    nested = projects["sample"]["root"] / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="disjoint physical project roots"):
        create_app(ORIGIN, accounts, {**projects, "other": dict(projects["other"], root=nested)})
    alias = tmp_path / "alias"
    alias.symlink_to(projects["other"]["root"], target_is_directory=True)
    with pytest.raises(ValueError, match="without symbolic links"):
        create_app(ORIGIN, accounts, {**projects, "other": dict(projects["other"], root=alias)})
    collision = dict(projects["sample"], artifacts={"audit": {"kind": "audit", "path": "audit.json"}},
                     targets=["audit:audit:retired:0"])
    with pytest.raises(ValueError, match="target namespace"):
        create_app(ORIGIN, accounts, {**projects, "sample": collision})


def test_cancelled_request_keeps_worker_slot_until_storage_finishes(tmp_path, monkeypatch):
    accounts, projects = settings(tmp_path)
    workspace = server.Workspace(ORIGIN, accounts, projects)
    monkeypatch.setattr(server, "MAX_WORKERS", 1)
    started, release = Event(), Event()

    def blocked_write():
        started.set()
        assert release.wait(5)
        return "stored"

    async def exercise():
        request = asyncio.create_task(workspace.storage(blocked_write))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(web.HTTPServiceUnavailable):
                await workspace.storage(lambda: "unexpected")
            worker, = workspace.workers
            release.set()
            assert await worker == "stored"
            assert await workspace.storage(lambda: "available") == "available"
        finally:
            release.set()

    asyncio.run(exercise())


def test_project_events_only_publish_committed_revisions_to_members(tmp_path, monkeypatch):
    accounts, projects = settings(tmp_path)
    monkeypatch.setattr(server, "MAX_PROJECT_SOCKETS", 2)
    events = "/projects/sample/events"

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                assert response.status == 200
                return {COOKIE: response.cookies[COOKIE].value}

            def connect(path, cookies, headers=HEADERS):
                cookie = "; ".join(f"{key}={value}" for key, value in cookies.items())
                return client.ws_connect(path, headers=dict(headers, Cookie=cookie))

            editor, viewer, outsider = [await login(actor) for actor in ("editor", "viewer", "outsider")]
            for headers, cookies, status in ((HEADERS, {}, 401), (HEADERS, outsider, 404),
                                             (dict(HEADERS, Origin="https://elsewhere.example.test"), viewer, 403)):
                with pytest.raises(WSServerHandshakeError) as error:
                    await connect(events, cookies, headers)
                assert error.value.status == status

            async with (
                connect(events, editor) as editor_ws,
                connect(events, viewer) as viewer_ws,
                connect("/projects/other/events", outsider) as other_ws,
            ):
                with pytest.raises(WSServerHandshakeError) as error:
                    await connect(events, viewer)
                assert error.value.status == 503
                prior = await client.get(REVIEW, headers=HEADERS, cookies=editor)
                headers = dict(HEADERS, **{"If-Match": prior.headers["ETag"]})
                body = {"target": TARGET, "operation_id": "operation-0000001", "patch": {"comment": "Private note"}}
                response = await client.patch(REVIEW, headers=headers, cookies=editor, json=body)
                assert response.status == 200
                etag = response.headers["ETag"]
                expected = {"type": "review", "etag": etag}
                assert await editor_ws.receive_json(timeout=1) == expected
                assert await viewer_ws.receive_json(timeout=1) == expected

                replay = await client.patch(REVIEW, headers=headers, cookies=editor, json=body)
                assert replay.status == 200
                for socket in (editor_ws, viewer_ws, other_ws):
                    with pytest.raises(asyncio.TimeoutError):
                        await socket.receive(timeout=0.05)

                await viewer_ws.close()
                async with connect(events, viewer):
                    snapshot = await client.get(REVIEW, headers=HEADERS, cookies=viewer)
                    assert snapshot.headers["ETag"] == etag
                    assert (await snapshot.json())["annotations"][TARGET]["comments"][0]["text"] == "Private note"

                logout = await client.delete("/session", headers=HEADERS, cookies=editor)
                assert logout.status == 204
                assert (await editor_ws.receive(timeout=1)).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)

    asyncio.run(exercise())


def test_workspace_audit_updates_recover_and_cannot_regress_after_restart(tmp_path, monkeypatch):
    accounts, projects = settings(tmp_path)
    monkeypatch.setattr(server, "ARTIFACT_POLL_SECONDS", 0.02)
    slot = {"kind": "audit", "path": "audit.json"}
    root = projects["sample"]["root"]
    projects["sample"]["artifacts"] = {"audit": slot}
    artifact_path = root / slot["path"]
    artifact_url = "/projects/sample/artifacts/audit"
    first_audit = {"findings": [{"severity": "warning", "message": "Old audit"}]}
    latest_audit = {"findings": [{"severity": "warning", "message": "Latest audit"}]}

    async def login(client, actor):
        response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
        assert response.status == 200
        return {COOKIE: response.cookies[COOKIE].value}

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            editor, outsider = [await login(client, actor) for actor in ("editor", "outsider")]
            denied = await client.get(artifact_url, headers=HEADERS, cookies=outsider)
            assert denied.status == 404
            cookie = f"{COOKIE}={editor[COOKIE]}"
            async with client.ws_connect("/projects/sample/events", headers=dict(HEADERS, Cookie=cookie)) as socket:
                await asyncio.to_thread(publish, root, slot, 1, first_audit)
                assert (await socket.receive_json(timeout=2))["type"] == "artifacts"
                first = await client.get(artifact_url, headers=HEADERS, cookies=editor)
                first_data = await first.json()
                assert first.status == 200 and first_data["freshness"] == "ready"
                original_bytes = artifact_path.read_bytes()

                artifact_path.write_bytes(b"{")
                notice = await socket.receive_json(timeout=2)
                assert notice["type"] == "artifacts"
                stale = await client.get(artifact_url, headers=HEADERS, cookies=editor)
                assert await stale.json() == dict(first_data, freshness="stale")
                blocked = await client.patch(REVIEW, headers=dict(HEADERS, **{"If-Match": notice["etag"]}), cookies=editor,
                                             json={"target": first_data["targets"][0], "operation_id": "operation-0000001",
                                                   "patch": {"comment": "Outdated"}})
                assert blocked.status == 400

                artifact_path.write_bytes(original_bytes)
                assert (await socket.receive_json(timeout=2))["type"] == "artifacts"
                recovered = await client.get(artifact_url, headers=HEADERS, cookies=editor)
                assert await recovered.json() == first_data
                await asyncio.to_thread(publish, root, slot, 2, latest_audit)
                notice = await socket.receive_json(timeout=2)
                newest = await client.get(artifact_url, headers=HEADERS, cookies=editor)
                newest_data = await newest.json()
                assert newest_data["generation"] == 2 and newest_data["data"] == latest_audit
                assert newest_data["targets"] != first_data["targets"]
                change = await client.patch(REVIEW, headers=dict(HEADERS, **{"If-Match": notice["etag"]}), cookies=editor,
                                            json={"target": newest_data["targets"][0], "operation_id": "operation-0000002",
                                                  "patch": {"comment": "Review this result."}})
                assert change.status == 200
                latest_bytes = artifact_path.read_bytes()

        for invalid in (b"{", original_bytes):
            artifact_path.write_bytes(invalid)
            async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as restarted:
                editor = await login(restarted, "editor")
                unavailable = await restarted.get(artifact_url, headers=HEADERS, cookies=editor)
                assert unavailable.status == 503 and await unavailable.json() == {"freshness": "unavailable"}
                review = await restarted.get(REVIEW, headers=HEADERS, cookies=editor)
                notes = (await review.json())["annotations"][newest_data["targets"][0]]["comments"]
                assert notes[0]["text"] == "Review this result."
        artifact_path.write_bytes(latest_bytes)
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as restarted:
            editor = await login(restarted, "editor")
            restored = await restarted.get(artifact_url, headers=HEADERS, cookies=editor)
            assert restored.status == 200 and await restored.json() == newest_data

    asyncio.run(exercise())
