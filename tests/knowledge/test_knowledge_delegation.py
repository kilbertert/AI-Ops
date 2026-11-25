"""D41：原Gateway＋目标客服读链＋实际HTTP知识替身，凭据不授予管理能力。"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import uvicorn
from dify.test_dify_knowledge_api import _client, _Runtime

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime
from aiops_diagnostics.knowledge import knowledge_delegation as delegation
from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application, load
from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable, MediaResourceSigner
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant


@pytest.fixture
def central(tmp_path):
    calls, hooks = [], {}
    image = b"\x89PNG\r\n\x1a\n" + b"fixture-image"
    video = b"\x00\x00\x00\x18ftypmp42" + b"fixture-video"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            tenant = self.headers.get("tenant-id")
            calls.append((self.command, self.path, tenant))
            if self.path == "/kb/knowledge-bases":
                body = json.dumps(
                    {"data": [{"id": "library", "name": tenant}, {"id": "hidden", "name": "private"}]}
                ).encode()
            else:
                body = image if "images" in self.path else video
                if hooks.get("media"):
                    hooks["media"]()
                total = hooks.get("large", len(body))
                request_range = self.headers.get("Range")
                hooks["last_range"] = request_range
                if request_range:
                    from aiops_diagnostics.knowledge.knowledge_retrieval import parse_byte_range

                    try:
                        begin, end = parse_byte_range(request_range, total)
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.end_headers()
                        return
                    body = b"z" * (end - begin + 1) if hooks.get("large") else body[begin : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {begin}-{end}/{total}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if hooks.get("large"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                    return  # client must reject headers before attempting to download the large body.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            tenant = self.headers.get("tenant-id")
            calls.append((self.command, self.path, tenant))
            query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(query) == {"question", "top_k"}
            if hooks.get("search"):
                hooks["search"]()
            if hooks.get("unavailable"):
                self.send_response(503)
                self.end_headers()
                return
            body = {
                "data": {
                    "chunks": [
                        {
                            "knowledge_base_id": "library",
                            "doc_id": "doc-image",
                            "id": "image-chunk",
                            "docnm_kwd": "guide.png",
                            "content": tenant + " help",
                            "image_id": "private-image",
                            "score": 0.9,
                        },
                        {
                            "knowledge_base_id": "library",
                            "doc_id": "doc-video",
                            "id": "video-chunk",
                            "docnm_kwd": "guide.mp4",
                            "doc_type": "video",
                            "content": "video guide",
                            "score": 0.8,
                        },
                    ]
                }
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    bt = threading.Thread(target=backend.serve_forever, daemon=True)
    bt.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    runtime = _Runtime(search=lambda *_: [], signer=MediaResourceSigner("synthetic-media"))
    runtime._media_proxy = None
    runtime.serve_media = GatewayRuntime.serve_media.__get__(runtime)
    client = _client(
        tmp_path,
        runtime,
        internal_token_secret="synthetic-central-secret",
        public_base_url=origin,
        kb_service_base_url=f"http://127.0.0.1:{backend.server_port}",
    )
    settings = client.app.state.gateway.settings
    runtime.gateway_settings = settings
    spaces = DifyWorkspaces(settings.database_file)
    apps = SelfServiceStore(settings.database_file)
    grants = KnowledgeGrants(settings.database_file)
    for env in ("a", "b"):
        spaces.claim("same", env, owner="test", lease_seconds=60)
        spaces.mark_created("same", env, "workspace-" + env)
        apps.register(
            app_id="app-" + env,
            tenant_id="same",
            workspace_id="workspace-" + env,
            agent_name="consumer-" + env,
            profile_id="",
            actor="test",
            entry="consumer",
            environment=env,
            targets={"knowledge_base_ids": ["library"]},
        )
        grants.grant("same", env, "library", "test")
    server = uvicorn.Server(uvicorn.Config(client.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    end = time.monotonic() + 5
    while not server.started and time.monotonic() < end:
        time.sleep(0.01)
    assert server.started
    yield SimpleNamespace(
        client=client,
        settings=settings,
        grants=grants,
        apps=apps,
        calls=calls,
        hooks=hooks,
        origin=origin,
        image=image,
        video=video,
        path=tmp_path,
    )
    server.should_exit = True
    thread.join(5)
    backend.shutdown()
    backend.server_close()
    bt.join(5)
    listener.close()


def target(c, env="a"):
    value = delegation.issue(c.settings, env, "same", "app-" + env, "library")
    file = c.path / ("target-" + env) / "knowledge.json"
    write_private_text(
        file,
        json.dumps({"schema": "aiops-knowledge-delegation/1", "base_url": c.origin, "capabilities": [value]}),
    )
    runtime = SimpleNamespace(
        gateway_settings=SimpleNamespace(
            knowledge_delegation_file=str(file), execution_environment=env, kb_service_timeout_seconds=2
        ),
        kb_search_client=None,
        media_signer=MediaResourceSigner("target-media-secret"),
    )
    return runtime, value


def test_original_consumer_read_chain_and_media_use_exact_environment(central):
    c = central
    for env in ("a", "b"):
        runtime, _ = target(c, env)
        rows = consumer_knowledge.owned_libraries(runtime, "same", "app-" + env)
        assert rows == [
            {"knowledge_base_id": "library", "name": knowledge_tenant("same", env), "audience": "customer"}
        ]
        binding = SimpleNamespace(
            agent_id="agent-a",
            agent_version_no=1,
            app_id="app-" + env,
            knowledge_base_ids=("library",),
            callback_version=5,
            content_file="",
            environment=env,
            content_snapshot={
                "schema": "aiops-consumer-live/1",
                "state": "published",
                "tenant_id": "same",
                "environment": env,
            },
        )
        caller = SimpleNamespace(effective_tenant_id="same")
        result = consumer_knowledge.select_context(
            runtime, caller, binding, "how to charge?", "en", "qa-local"
        )
        assert result["status"] == "found"
        assert knowledge_tenant("same", env) in result["sources"][0]["content"]
        media = result["sources"][0]["media"][0]
        grant = runtime.media_signer.verify_signed(media["url"])
        assert grant.session_id == "qa-local" and grant.agent_version == "consumer:agent-a#v1"
        assert for_application(runtime, "same", "app-" + env).fetch_media(grant).body == c.image
        movie = result["sources"][1]["media"][0]
        grant = runtime.media_signer.verify_signed(movie["url"])
        assert (
            for_application(runtime, "same", "app-" + env).fetch_media(grant, range_header="bytes=0-3").body
            == c.video[:4]
        )
        assert all(call[2] == knowledge_tenant("same", env) for call in c.calls)
        c.calls.clear()


def test_revocation_regrant_cannot_restore_old_token_or_media(central):
    c = central
    runtime, original = target(c)
    remote = for_application(runtime, "same", "app-a")
    media = remote.search(("library",), "help", 2).chunks[0].media[0].resource.url
    c.grants.revoke("same", "a", "library")
    with pytest.raises(KnowledgeSearchUnavailable):
        remote.list_knowledge_bases()
    assert c.client.get(media.removeprefix(c.origin)).status_code == 403
    c.grants.grant("same", "a", "library", "test-again")
    with pytest.raises(KnowledgeSearchUnavailable):
        remote.search(("library",), "help", 2)
    assert c.client.get(media.removeprefix(c.origin)).status_code == 403
    runtime, fresh = target(c)
    assert fresh != original
    assert for_application(runtime, "same", "app-a").search(("library",), "help", 2).status == "found"


def test_cross_application_library_and_forged_scopes_fail_closed(central):
    c = central
    runtime, token = target(c)
    with pytest.raises(KnowledgeSearchUnavailable):
        for_application(runtime, "same", "app-b")
    remote = for_application(runtime, "same", "app-a")
    with pytest.raises(KnowledgeSearchUnavailable):
        remote.search(("hidden",), "help", 2)
    assert c.calls == []
    scope = delegation.parse_id(token["knowledge_id"])
    for index, value in ((0, "b"), (1, "other"), (2, "workspace-b"), (3, "app-b"), (4, "hidden")):
        changed = list(scope)
        changed[index] = value
        # Even a correctly signed but no-longer-owned tuple must be refused.
        key = delegation.credential(c.settings.internal_token_secret, changed)
        response = c.client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer " + key})
        assert response.status_code == 403
    response = c.client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer " + token["token"] + "a"})
    assert response.status_code == 403 and c.calls == []


def test_revocation_during_fetch_and_upstream_failure_are_not_empty_success(central):
    c = central
    runtime, _ = target(c)
    remote = for_application(runtime, "same", "app-a")
    media = remote.search(("library",), "help", 2).chunks[0].media[0].resource.url
    c.hooks["media"] = lambda: c.grants.revoke("same", "a", "library")
    assert c.client.get(media.removeprefix(c.origin)).status_code == 403
    c.hooks.clear()
    c.grants.grant("same", "a", "library", "test")
    runtime, _ = target(c)
    remote = for_application(runtime, "same", "app-a")
    c.hooks["unavailable"] = True
    with pytest.raises(KnowledgeSearchUnavailable):
        remote.search(("library",), "help", 2)
    c.hooks.clear()
    c.hooks["search"] = lambda: c.grants.revoke("same", "a", "library")
    with pytest.raises(KnowledgeSearchUnavailable):
        remote.search(("library",), "help", 2)


def test_private_target_configuration_rejects_wrong_environment_or_permissions(central):
    runtime, _ = target(central)
    path = runtime.gateway_settings.knowledge_delegation_file
    with pytest.raises(ValueError):
        load(path, "b")
    from pathlib import Path

    Path(path).chmod(0o644)
    with pytest.raises(RuntimeError):
        load(path, "a")


def test_export_command_only_writes_new_private_capability(central, monkeypatch):
    from typer.testing import CliRunner

    from aiops_diagnostics import admin_cli

    c = central
    monkeypatch.setattr(admin_cli, "_settings_with_config_file", lambda _: c.settings)
    file = c.path / "export" / "capability.json"
    args = [
        "knowledge-delegation",
        "--environment",
        "a",
        "--tenant",
        "same",
        "--app",
        "app-a",
        "--library",
        "library",
        "--output",
        str(file),
    ]
    runner = CliRunner()
    result = runner.invoke(admin_cli.admin_app, args)
    assert result.exit_code == 0, result.output
    assert "kdx_" not in result.output and "synthetic-central" not in file.read_text()
    assert file.stat().st_mode & 0o777 == 0o600
    assert load(file, "a")[1][("same", "app-a")][0][4] == "library"
    original = file.read_text()
    assert runner.invoke(admin_cli.admin_app, args).exit_code == 1
    assert file.read_text() == original
    c.grants.revoke("same", "a", "library")
    missing = file.with_name("denied.json")
    assert runner.invoke(admin_cli.admin_app, args[:-1] + [str(missing)]).exit_code == 1
    assert not missing.exists()


def test_generation_cannot_reuse_old_epoch_when_clock_moves_back(central, monkeypatch):
    from aiops_diagnostics.consumer import knowledge_access as consumer_knowledge_grants

    c = central
    old = delegation.issue(c.settings, "a", "same", "app-a", "library")
    c.grants.revoke("same", "a", "library")
    with monkeypatch.context() as patch:
        patch.setattr(consumer_knowledge_grants.time, "time", lambda: 1)
        c.grants.grant("same", "a", "library", "new-explicit-approval")
    scope = delegation.parse_id(old["knowledge_id"])
    with pytest.raises(delegation.DelegationRefused):
        delegation.active(c.settings, scope)


def test_target_startup_rejects_mixed_or_invalid_knowledge_configuration(central):
    from dataclasses import replace

    from aiops_diagnostics.gateway.gateway_api import create_gateway_app

    runtime, _ = target(central)
    settings = replace(
        central.settings,
        knowledge_delegation_file=runtime.gateway_settings.knowledge_delegation_file,
        execution_environment="a",
    )
    with pytest.raises(ValueError, match="shared kb-service"):
        settings.validate()
    settings.kb_service_base_url = ""
    settings.execution_environment = "b"
    with pytest.raises(ValueError, match="scope mismatch"):
        create_gateway_app(settings=settings)


def test_original_http_agent_answer_and_media_recheck_local_admission(central, tmp_path, monkeypatch):
    import uuid

    from consumer.test_consumer_agent import ask, wire
    from consumer.test_consumer_chat_service import Client, done
    from execution.test_app_runtime_dify_entry import dify_chat

    from aiops_diagnostics.dify.dify_chatflow import ChatflowReply

    c = central
    tenant, environment = "TENANT-A", "env-a"
    app = "11111111-2222-3333-4444-555555555555"
    workspace = "workspace-target"
    spaces = DifyWorkspaces(c.settings.database_file)
    spaces.claim(tenant, environment, owner="test", lease_seconds=60)
    spaces.mark_created(tenant, environment, workspace)
    c.apps.register(
        app_id=app,
        tenant_id=tenant,
        workspace_id=workspace,
        agent_name="consumer",
        profile_id="",
        actor="test",
        entry="consumer",
        environment=environment,
        targets={"knowledge_base_ids": ["library"]},
    )
    c.grants.grant(tenant, environment, "library", "test")
    capability = delegation.issue(c.settings, environment, tenant, app, "library")
    (tmp_path / "target").mkdir()
    with dify_chat(tmp_path / "target", monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path / "target")
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0]["knowledge_base_ids"] = ["library"]
        env["bindings"].write_text(json.dumps(doc))
        file = tmp_path / "target" / "private" / "knowledge.json"
        write_private_text(
            file,
            json.dumps(
                {"schema": "aiops-knowledge-delegation/1", "base_url": c.origin, "capabilities": [capability]}
            ),
        )
        runtime.gateway_settings.knowledge_delegation_file = str(file)
        runtime.kb_search_client = None

        class Agent(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                response = env["client"].post(
                    "/v1/internal/consumer-data/agent",
                    headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                    json={
                        "resource": "knowledge",
                        "query": "help",
                        "order_no": "",
                        "knowledge_base_ids": ["library"],
                    },
                )
                assert response.status_code == 200 and response.json()["status"] == "found", response.text
                return ChatflowReply(*ids, "Follow the charger guide.", {})

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Agent)
        response = ask(env, "how to charge?")
        assert response.status_code == 202, response.text
        observed = done(env, response.json()["qa_id"])
        assert observed.status_code == 200 and observed.json()["status"] == "completed", observed.text
        media = [b["media"] for b in observed.json()["result"]["blocks"] if b["kind"] in {"image", "video"}]
        assert len(media) == 2 and all(m["url"].startswith("/v1/media/") for m in media)
        read = env["client"].get(media[0]["url"])
        assert read.status_code == 200 and read.content == c.image
        movie = env["client"].get(media[1]["url"], headers={"Range": "bytes=0-3"})
        assert movie.status_code == 206 and movie.content == c.video[:4]
        seek = env["client"].get(media[1]["url"], headers={"Range": "bytes=4-7"})
        assert seek.status_code == 206 and seek.content == c.video[4:8]
        suffix = env["client"].get(media[1]["url"], headers={"Range": "bytes=-4"})
        assert suffix.status_code == 206 and suffix.content == c.video[-4:]
        c.hooks["large"] = 40 * 1024 * 1024
        large = env["client"].get(media[1]["url"], headers={"Range": "bytes=33554440-33554443"})
        assert large.status_code == 206 and large.content == b"zzzz"
        assert large.headers["Content-Range"] == "bytes 33554440-33554443/41943040"
        assert c.hooks["last_range"] == "bytes=33554440-33554443"
        end = env["client"].get(media[1]["url"], headers={"Range": "bytes=-7"})
        assert end.status_code == 206 and end.content == b"z" * 7
        assert end.headers["Content-Range"] == "bytes 41943033-41943039/41943040"
        full_large = env["client"].get(media[1]["url"])
        assert full_large.status_code == 503 and full_large.content == b""
        c.hooks.pop("large")
        runtime.gateway_settings.consumer_chatflow_enabled = False
        before = len(c.calls)
        assert env["client"].get(media[0]["url"]).status_code == 403
        assert len(c.calls) == before


def test_old_opaque_shared_key_prefix_still_uses_exact_match(tmp_path):
    key = "kdx_existing-opaque-secret-at-least-32chars"
    client = _client(tmp_path, _Runtime(), dify_knowledge_api_key=key)
    response = client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer " + key})
    assert response.status_code == 200 and response.json()["total"] == 2


def test_unknown_length_oversize_body_is_rejected_not_truncated():
    from io import BytesIO

    from aiops_diagnostics.knowledge.knowledge_retrieval import read_media_response

    response = BytesIO(b"0123456789")
    response.status = 200
    response.headers = {}
    with pytest.raises(KnowledgeSearchUnavailable, match="too large"):
        read_media_response(response, max_bytes=8)


@pytest.mark.parametrize(
    "status,content_range,body",
    [(206, "bytes 4-7/100", b"xx"), (206, "bytes 3-6/100", b"xxxx"), (206, "", b"xxxx"), (201, "", b"xxxx")],
)
def test_explicit_partial_response_must_match_request(status, content_range, body):
    from aiops_diagnostics.knowledge.knowledge_retrieval import MediaResponse, media_response

    grant = SimpleNamespace(mime_type="video/mp4")
    with pytest.raises(KnowledgeSearchUnavailable):
        media_response(grant, MediaResponse(status, {"Content-Range": content_range}, body), "bytes=4-7")
