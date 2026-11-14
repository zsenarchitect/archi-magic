import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
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


def test_browser_workspace_serves_installed_shell_and_authorized_review_rows(tmp_path):
    import base64
    import re

    accounts, projects = settings(tmp_path)
    root = projects["sample"]["root"]
    slot = {"kind": "preview", "path": "preview.json"}
    projects["sample"]["artifacts"] = {"latest": slot}
    identifier = 9007199254740993
    message = "</script><img src=x onerror=alert(1)>"
    publish(root, slot, 1, {"columns": ["Element ID", "Text"], "before": [[identifier, "Old"]],
                           "after": [[identifier, message]], "review_only": True,
                           "deletions": [{"id": identifier, "category": "Detail Lines", "name": message,
                                          "script": "private execution field"}]})
    project = "/projects/sample"
    snapshot = project + "/workspace"

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            redirect = await client.get(project, headers=HEADERS, allow_redirects=False)
            assert redirect.status == 302 and redirect.headers["Location"] == "/?project=sample"
            assert (await client.get(snapshot, headers=HEADERS)).status == 401
            for actor, expected in (("outsider", 404), ("viewer", 200)):
                login = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                cookie = {COOKIE: login.cookies[COOKIE].value}
                shell = await client.get(project, headers=HEADERS, cookies=cookie)
                data = await client.get(snapshot, headers=HEADERS, cookies=cookie)
                assert shell.status == data.status == expected
                if expected != 200:
                    continue
                html = await shell.text()
                assert message not in html
                script, = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                policy = shell.headers["Content-Security-Policy"]
                assert f"script-src 'sha256-{digest}'" in policy
                assert f"connect-src {ORIGIN} wss://review.example.test;" in policy
                assert "frame-ancestors 'none'" in policy
                result = await data.json()
                assert result["etag"] == data.headers["ETag"]
                assert result["role"] == actor
                artifact = result["artifacts"]["latest"]
                assert artifact["freshness"] == "ready"
                assert artifact["notices"] == [
                    "This preview is review only.", "Delete: 1 element",
                ]
                published = await (await client.get(project + "/artifacts/latest", headers=HEADERS,
                                                    cookies=cookie)).json()
                assert published["data"]["deletions"] == [
                    {"id": str(identifier), "category": "Detail Lines", "name": message},
                ]
                target, deletion_target = published["targets"]
                assert artifact["rows"] == [{
                    "target": target, "title": "Change 1",
                    "details": [f"Before · Element ID = {identifier} · Text = Old",
                                f"After · Element ID = {identifier} · Text = {message}"],
                }, {
                    "target": deletion_target, "title": f"Delete element {identifier}",
                    "details": [f"Detail Lines · {message}"],
                }]

    asyncio.run(exercise())


def test_deletion_only_preview_accepts_team_review_on_exact_element(tmp_path):
    accounts, projects = settings(tmp_path)
    root = projects["sample"]["root"]
    slot = {"kind": "preview", "path": "preview.json"}
    projects["sample"]["artifacts"] = {"latest": slot}
    identifier = "9007199254740993"
    publish(root, slot, 1, {"kind": "change-preview", "columns": ["Element ID"], "before": [], "after": [],
                           "deletions": [{"id": identifier, "category": "Detail Lines", "name": "Grid line"}]})

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            cookies = {}
            for actor in ("editor", "viewer"):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                assert response.status == 200
                cookies[actor] = {COOKIE: response.cookies[COOKIE].value}
            response = await client.get("/projects/sample/workspace", headers=HEADERS, cookies=cookies["editor"])
            assert response.status == 200
            snapshot = await response.json()
            artifact = snapshot["artifacts"]["latest"]
            row, = artifact["rows"]
            assert row["title"] == "Delete element " + identifier
            assert row["details"] == ["Detail Lines · Grid line"]
            body = {"target": row["target"], "operation_id": "operation-delete-0001",
                    "patch": {"comment": "Keep this grid line in the Revit model."}}
            headers = dict(HEADERS, **{"If-Match": snapshot["etag"]})
            assert (await client.patch(REVIEW, headers=headers, cookies=cookies["viewer"], json=body)).status == 403
            assert (await client.patch(REVIEW, headers=headers, cookies=cookies["editor"], json=body)).status == 200
            review = await client.get(REVIEW, headers=HEADERS, cookies=cookies["viewer"])
            annotations = (await review.json())["annotations"]
            assert annotations[row["target"]]["comments"][0]["text"] == body["patch"]["comment"]

    asyncio.run(exercise())


def test_model_viewer_is_built_offline_and_served_only_to_project_members(tmp_path):
    accounts, projects = settings(tmp_path)
    root = projects["sample"]["root"]
    slot = {"kind": "model", "path": "model.json"}
    projects["sample"]["artifacts"] = {"coordination": slot}
    identifier = 9007199254740993
    title = 'Title </script><img src=x> \\ \n'
    model = {"schema": "am-bim-viewer/1", "source": {"title": title}, "elements": [{
        "id": identifier, "name": "__TITLE__", "mesh": {
            "vertices": [0, 0, 0, 1, 0, 0, 0, 1, 0], "indices": [0, 1, 2],
        },
    }]}
    publish(root, slot, 1, model)
    project = "/projects/sample"
    snapshot = project + "/workspace"

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                return {COOKIE: response.cookies[COOKIE].value}

            viewer, outsider = [await login(actor) for actor in ("viewer", "outsider")]

            async def model_entry():
                response = await client.get(snapshot, headers=HEADERS, cookies=viewer)
                assert response.status == 200
                return (await response.json())["artifacts"]["coordination"]

            entry = await model_entry()
            assert entry["freshness"] == "ready" and entry["rows"] == []
            url = project + "/models/coordination/" + entry["viewer_digest"] + ".html"
            assert (await client.get(url, headers=HEADERS)).status == 401
            for method in (client.get, client.head):
                assert (await method(url, headers=HEADERS, cookies=outsider)).status == 404
            response = await client.get(url, headers=HEADERS, cookies=viewer)
            assert response.status == 200 and response.content_type == "text/html"
            assert response.headers["Cache-Control"] == "private, no-store"
            policy = response.headers["Content-Security-Policy"]
            assert "sandbox allow-scripts;" in policy and "connect-src 'none'" in policy
            payload = await response.read()
            assert hashlib.sha256(payload).hexdigest() == entry["viewer_digest"]
            assert b'data:text/javascript;base64,' in payload
            assert b'</script><img src=x>' not in payload
            assert b'"name":"__TITLE__"' in payload
            assert f'"id":"{identifier}"'.encode() in payload
            assert (await client.head(url, headers=HEADERS, cookies=viewer)).headers["Content-Length"] == str(len(payload))
            partial = await client.get(url, headers=dict(HEADERS, Range="bytes=0-14"), cookies=viewer)
            assert partial.status == 206 and await partial.read() == payload[:15]
            raw = await client.get(project + "/artifacts/coordination", headers=HEADERS, cookies=viewer)
            assert await raw.json() == entry

            path = root / "model.json"
            original = path.read_bytes()
            path.unlink()
            path.symlink_to("elsewhere.json")
            assert (await model_entry())["freshness"] == "stale"
            assert await (await client.get(url, headers=HEADERS, cookies=viewer)).read() == payload
            path.unlink()
            with path.open("wb") as sparse:
                sparse.truncate(256 * 1024 * 1024 + 1)
            assert (await model_entry())["freshness"] == "stale"
            path.write_bytes(original)
            assert await model_entry() == entry

            publish(root, slot, 2, dict(model, source={"title": "Replacement"}))
            newer = await model_entry()
            assert newer["viewer_digest"] != entry["viewer_digest"]
            assert (await client.get(url, headers=HEADERS, cookies=viewer)).status == 404
            path.write_bytes(original)

        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as restarted:
            response = await restarted.post("/session", headers=HEADERS, json={"token": TOKENS["viewer"]})
            viewer = {COOKIE: response.cookies[COOKIE].value}
            read = await restarted.get(snapshot, headers=HEADERS, cookies=viewer)
            assert (await read.json())["artifacts"]["coordination"]["freshness"] == "unavailable"
            assert (await restarted.get(url, headers=HEADERS, cookies=viewer)).status == 404

    asyncio.run(exercise())


def test_audit_report_keeps_project_provenance_and_excludes_source_text(tmp_path, monkeypatch):
    health_scripts = Path(__file__).resolve().parents[2] / "am-model-health" / "scripts"
    monkeypatch.syspath_prepend(str(health_scripts))
    from dwg_cad_audit import check_xrefs

    accounts, projects = settings(tmp_path)
    config = projects["sample"]
    slots = {name: {"kind": "audit", "path": name + ".json"} for name in ("cad", "health", "missing")}
    config["artifacts"] = slots
    secret = r"Z:\private\missing.dwg"
    sentinel = "<script>private-source-text</script>"
    findings = check_xrefs([{"name": sentinel, "path": secret, "status": "missing"}])
    assert secret in findings[0]["message"] and sentinel in findings[0]["message"]
    publish(config["root"], slots["cad"], 3, {"findings": findings})
    publish(config["root"], slots["health"], 4, {"model": sentinel, "timestamp": "2026-01-01T10:00:00", "metrics": [
        {"id": "MH-02", "label": "Warnings", "unit": "", "hint": sentinel, "value": 2, "status": "warn"},
    ]})
    project = "/projects/sample"
    report = project + "/audit-report"
    started, finish = Event(), Event()
    build = server.build_audit_report

    def held_report(*args):
        started.set()
        if not finish.wait(5):
            raise RuntimeError("Audit report test did not release the worker")
        return build(*args)

    async def exercise():
        disconnected = asyncio.Event()
        read_report = server.Workspace.read_audit_report

        async def observe_disconnect(workspace, request):
            try:
                return await read_report(workspace, request)
            except asyncio.CancelledError:
                disconnected.set()
                raise

        monkeypatch.setattr(server.Workspace, "read_audit_report", observe_disconnect)
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                return {COOKIE: response.cookies[COOKIE].value}

            viewer, outsider = [await login(actor) for actor in ("viewer", "outsider")]
            assert (await client.get(report, headers=HEADERS)).status == 401
            assert (await client.head(report, headers=HEADERS, cookies=outsider)).status == 404
            await (await client.get(project + "/workspace", headers=HEADERS, cookies=viewer)).read()
            monkeypatch.setattr(server, "build_audit_report", held_report)
            request = asyncio.create_task(client.get(report, headers=HEADERS, cookies=viewer))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                busy = await client.get(report, headers=HEADERS, cookies=viewer)
                assert busy.status == 503 and busy.headers["Retry-After"]
                other = await client.get("/projects/other/review", headers=HEADERS, cookies=outsider)
                edit = await client.patch("/projects/other/review", cookies=outsider,
                                          headers=dict(HEADERS, **{"If-Match": other.headers["ETag"]}),
                                          json={"target": TARGET, "operation_id": "operation-0000001",
                                                "patch": {"comment": "Review remains available."}})
                assert edit.status == 200
            finally:
                finish.set()
            response = await request
            assert response.status == 200 and response.headers["Cache-Control"] == "private, no-store"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
            html = await response.text()
            assert "<td>cad</td><td>3</td><td>ready</td>" in html
            assert "<td>health</td><td>4</td><td>ready</td><td>2026-01-01T10:00:00</td>" in html
            assert "<td>missing</td><td>Unavailable</td><td>unavailable</td>" in html
            assert "CAD-XREF-01" in html and "Model health audits" in html and "MH-02" in html
            assert secret not in html and "private-source-text" not in html
            selected = await client.get(report + "?slot=health", headers=HEADERS, cookies=viewer)
            assert "CAD-XREF-01" not in await selected.text()

            started.clear()
            finish.clear()
            abandoned = asyncio.create_task(client.get(report, headers=HEADERS, cookies=viewer))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                abandoned.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await abandoned
                await asyncio.wait_for(disconnected.wait(), 2)
                busy = await client.get(report, headers=HEADERS, cookies=viewer)
                assert busy.status == 503
            finally:
                finish.set()

        config["audit_reports"] = False
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as disabled:
            response = await disabled.post("/session", headers=HEADERS, json={"token": TOKENS["viewer"]})
            viewer = {COOKIE: response.cookies[COOKIE].value}
            for method in (disabled.get, disabled.head):
                assert (await method(report, headers=HEADERS, cookies=viewer)).status == 404

    asyncio.run(exercise())


def test_document_pages_hold_provenance_and_reject_changed_cursor(tmp_path):
    accounts, projects = settings(tmp_path)
    project = projects["sample"]
    slot = {"kind": "document", "path": "rooms.json"}
    project["artifacts"] = {"rooms": slot}
    raw = {"source": {"file": "Rooms.xlsx", "format": "xlsx", "sheet": "Room Data", "header_row": 1,
                      "private_path": "/private/workbook.xlsx"},
           "columns": ["Room", "Finish"], "rows": [{"source_row": index, "cells": [str(index), "<img src=x>"]}
                                                   for index in range(2, 57)]}
    publish(project["root"], slot, 1, raw)
    url = "/projects/sample/documents/rooms/rows"

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                return {COOKIE: response.cookies[COOKIE].value}

            viewer, outsider = [await login(actor) for actor in ("viewer", "outsider")]

            async def read_page(**kwargs):
                # Artifact refresh shares the document worker. Retry only its
                # explicit busy response, never publication failures.
                for _ in range(20):
                    response = await client.get(url, headers=HEADERS, cookies=viewer, **kwargs)
                    if response.status != 503 or response.headers.get("Retry-After") != "2":
                        return response
                    await response.read()
                    await asyncio.sleep(0.05)
                pytest.fail("The document worker did not become available.")

            assert (await client.get(url, headers=HEADERS)).status == 401
            for method in (client.get, client.head):
                assert (await method(url, headers=HEADERS, cookies=outsider)).status == 404
            response = await read_page()
            assert response.status == 200
            first = await response.json()
            assert first["columns"] == raw["columns"]
            assert first["rows"] == raw["rows"][:50]
            cursor = {"digest": first["digest"], "generation": first["generation"], "index": first["next_index"]}
            response = await read_page(params=cursor)
            assert response.status == 200
            second = await response.json()
            assert second["rows"] == raw["rows"][50:] and second["next_index"] is None

            metadata = await client.get("/projects/sample/artifacts/rooms", headers=HEADERS, cookies=viewer)
            entry = await metadata.json()
            assert entry["rows"] == [] and entry["document"]["collections"] == {"rows": 55}
            assert entry["document"]["source"] == {"file": "Rooms.xlsx", "format": "xlsx", "sheet": "Room Data",
                                                   "header_row": 1}
            assert "private_path" not in await metadata.text()
            path = project["root"] / slot["path"]
            original = path.read_bytes()
            path.unlink()
            path.symlink_to("elsewhere.json")
            stale = await read_page(params=cursor)
            assert stale.status == 200
            assert (await stale.json())["freshness"] == "stale"
            path.unlink()
            path.write_bytes(original)
            publish(project["root"], slot, 2, {**raw, "rows": raw["rows"][1:]})
            response = await read_page(params=cursor)
            assert response.status == 409
            current = await read_page()
            assert current.status == 200
            assert (await current.json())["rows"] == raw["rows"][1:51]

    asyncio.run(exercise())


def test_document_worker_admission_survives_a_client_disconnect(tmp_path, monkeypatch):
    accounts, projects = settings(tmp_path)
    project = projects["sample"]
    slot = {"kind": "document", "path": "report.json"}
    project["artifacts"] = {"document": slot}
    publish(project["root"], slot, 1, {"source": {"file": "Schedule.docx", "format": "docx", "table": 1, "header_row": 1},
                                     "columns": ["Room"], "rows": [{"source_row": 2, "cells": ["001"]}]})
    started, finish = Event(), Event()
    copy_document = server.copy_document

    def held_copy(data):
        started.set()
        if not finish.wait(5):
            raise RuntimeError("Document test did not release the worker")
        return copy_document(data)

    monkeypatch.setattr(server, "copy_document", held_copy)

    async def exercise():
        disconnected = asyncio.Event()
        read_document = server.Workspace.read_document

        async def observe_disconnect(workspace, request):
            try:
                return await read_document(workspace, request)
            except asyncio.CancelledError:
                disconnected.set()
                raise

        monkeypatch.setattr(server.Workspace, "read_document", observe_disconnect)
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                return {COOKIE: response.cookies[COOKIE].value}

            viewer, outsider = [await login(actor) for actor in ("viewer", "outsider")]
            url = "/projects/sample/documents/document/rows"
            abandoned = asyncio.create_task(client.get(url, headers=HEADERS, cookies=viewer))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                abandoned.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await abandoned
                await asyncio.wait_for(disconnected.wait(), 2)
                busy = await client.get(url, headers=HEADERS, cookies=viewer)
                assert busy.status == 503 and busy.headers["Retry-After"] == "2"
                review = "/projects/other/review"
                other = await client.get(review, headers=HEADERS, cookies=outsider)
                saved = await client.patch(review, headers=dict(HEADERS, **{"If-Match": other.headers["ETag"]}),
                                           cookies=outsider, json={"target": TARGET, "operation_id": "operation-0000001",
                                                                  "patch": {"comment": "Review is available."}})
                assert saved.status == 200
            finally:
                finish.set()

    asyncio.run(exercise())


def test_saved_recipe_catalog_uses_a_private_exact_source_binding(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "am-safe-change" / "scripts"))
    from recipe_library import save_recipe
    from workspace_artifacts import read_published
    from workspace_review_state import canonical

    accounts, projects = settings(tmp_path)
    project = projects["sample"]
    slot = {"kind": "recipe", "path": "catalog.json"}
    project["artifacts"] = {"placement": slot}
    source = tmp_path / "operator"
    code = "raise RuntimeError('private source sentinel')"
    description = "Review <img src=x onerror=alert(1)> placement"
    _, filename = save_recipe(str(source), "Place Views", description, code,
                              inputs_schema={"private_schema_sentinel": r"Z:\private\config"}, author="Coordinator",
                              tested_years=["2026"])
    recipe = Path(filename)
    config = tmp_path / "workspace.json"
    config.write_text(json.dumps({"projects": {"sample": {**project, "root": str(project["root"])}}}))
    publisher = Path(server.__file__).with_name("workspace_artifacts.py")
    command = [sys.executable, str(publisher), str(config), "sample", "placement", "1", str(recipe)]
    publish_result = subprocess.run(command, capture_output=True, text=True)
    assert publish_result.returncode == 0, publish_result.stderr
    artifact = read_published(project["root"], slot)
    source_digest = hashlib.sha256(recipe.read_bytes()).hexdigest()
    assert artifact["data"]["recipe_sha256"] == source_digest
    assert code not in str(artifact) and "private_schema_sentinel" not in str(artifact)
    output_path = project["root"] / slot["path"]
    previous = output_path.read_bytes()
    recipe.write_bytes(recipe.read_bytes() + b"\n")
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert output_path.read_bytes() == previous

    async def exercise():
        async with TestClient(TestServer(create_app(ORIGIN, accounts, projects))) as client:
            async def login(actor):
                response = await client.post("/session", headers=HEADERS, json={"token": TOKENS[actor]})
                return {COOKIE: response.cookies[COOKIE].value}

            viewer, outsider = [await login(actor) for actor in ("viewer", "outsider")]
            project_path = "/projects/sample"
            legacy = project_path + "/artifacts/placement"
            assert (await client.get(legacy, headers=HEADERS)).status == 401
            for method in (client.get, client.head):
                assert (await method(legacy, headers=HEADERS, cookies=outsider)).status == 404
            expected = {"kind": "recipe", "generation": 1, "freshness": "ready", "rows": [], "recipe": {
                "name": "Place Views", "slug": "place_views", "description": description,
                "author": "Coordinator", "version": "1.0.0", "tested_years": ["2026"],
            }}
            current = await client.get(legacy, headers=HEADERS, cookies=viewer)
            assert await current.json() == expected
            assert (await client.head(legacy, headers=HEADERS, cookies=viewer)).status == 200
            snapshot = await client.get(project_path + "/workspace", headers=HEADERS, cookies=viewer)
            assert (await snapshot.json())["artifacts"]["placement"] == expected
            assert source_digest not in await snapshot.text() and "private_schema_sentinel" not in await snapshot.text()

            artifact["data"]["metadata"]["script"] = code
            content = {key: artifact[key] for key in ("kind", "data", "board")}
            artifact["generation"] = 2
            artifact["digest"] = hashlib.sha256(canonical(content)).hexdigest()
            output_path.write_bytes(canonical(artifact))
            response = await client.get(legacy, headers=HEADERS, cookies=viewer)
            assert await response.json() == dict(expected, freshness="stale")

    asyncio.run(exercise())
