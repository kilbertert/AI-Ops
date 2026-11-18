"""D26：候选知识复合身份、未知创建与原任务恢复的行为判据。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.knowledge.kb_onboarding import derive_email, encrypt_credential
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant
from gateway.test_environment_tasks import prepare_http, prepared, workspace_job  # noqa: F401


@pytest.fixture
def knowledge(prepared, workspace_upstream, monkeypatch):  # noqa: F811
    path, reference, client, settings = prepared
    workspace, _ = workspace_job(prepared, workspace_upstream)
    assert prepare_http(prepared, workspace).json()["state"] == "observed"
    calls, libraries = [], {}
    lose = [False]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            scope = self.headers["tenant-id"]
            calls.append(("GET", self.path, scope, None))
            self.reply({"data": libraries.get(scope, [])})

        def do_POST(self):
            scope = self.headers["tenant-id"]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, scope, body))
            if self.path == "/kb/knowledge-bases":
                row = {"id": "kb-" + str(len(calls)), "name": body["name"]}
                libraries.setdefault(scope, []).append(row)
                self.reply({"data": {} if lose[0] else row})
            else:
                self.reply({"data": {"matches": True}})

        def reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    settings.kb_service_base_url = f"http://127.0.0.1:{server.server_port}"
    settings.ragflow_credential_key = "0123456789abcdef"
    settings.ragflow_embedding_base_url = "https://embedding.example.test/v1"
    settings.ragflow_embedding_api_key = "fixture"
    settings.ragflow_embedding_model = "fixture"
    settings.ragflow_tenant_domain = "kb.internal"
    context = SimpleNamespace(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(kb_search_client=object()),
    )
    monkeypatch.setattr("aiops_diagnostics.knowledge.kb_onboarding.verify_embedding", lambda **_: 3)
    # HTTP routes close over the original context; provide its store/runtime just as Gateway does.
    from aiops_diagnostics.dify import environment_knowledge

    real = environment_knowledge.run
    monkeypatch.setattr(environment_knowledge, "run", lambda _, *args: real(context, *args))
    try:
        yield prepared, workspace, calls, libraries, lose, context
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def submit(knowledge, workspace=None):  # noqa: F811
    fixture, source, *_ = knowledge
    response = fixture[2].post(
        "/v1/dify/management/environment-tasks",
        json={"workspace_task": (workspace or source)["id"], "tenant_id": "same"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute(knowledge, job):  # noqa: F811
    fixture = knowledge[0]
    payload = {"id": job["id"], "owner": "c" * 32, "prepare": "knowledge"}
    tasks.worker(fixture[0], fixture[1], {"id": job["id"], "owner": payload["owner"]})
    return (
        fixture[2]
        .post(
            "/v1/internal/environment-tasks",
            json=payload,
            headers=build_internal_token_headers(fixture[3].environment_task_key, 300, int(time.time())),
        )
        .json()
    )


def test_knowledge_is_scoped_and_new_plan_reuses_empty_library(knowledge, workspace_upstream):  # noqa: F811
    fixture, workspace, calls, _, _, _ = knowledge
    first = submit(knowledge)
    assert submit(knowledge)["id"] == first["id"]
    result = execute(knowledge, first)
    assert result["state"] == "observed" and result["executable"] is False
    assert [c["id"] for c in result["result"]["checks"] if c["state"] == "passed"] == [
        "workspace",
        "knowledge",
    ]
    assert {c[2] for c in calls} == {knowledge_tenant("same", "a")}
    other, _ = workspace_job(fixture, workspace_upstream, "b")
    assert prepare_http(fixture, other).json()["state"] == "observed"
    second = execute(knowledge, submit(knowledge, other))
    assert second["result"]["knowledge_base_id"] != result["result"]["knowledge_base_id"]
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    newer, _ = workspace_job(fixture, workspace_upstream)
    assert prepare_http(fixture, newer).json()["state"] == "observed"
    reused = execute(knowledge, submit(knowledge, newer))
    assert reused["result"]["knowledge_base_id"] == result["result"]["knowledge_base_id"]
    assert len([c for c in calls if c[:2] == ("POST", "/kb/knowledge-bases")]) == 2


def test_new_plan_after_key_rotation_and_rename_reuses_confirmed_library(knowledge, workspace_upstream):  # noqa: F811
    fixture, _, calls, libraries, _, context = knowledge
    first = execute(knowledge, submit(knowledge))
    old = first["result"]["knowledge_base_id"]
    libraries[knowledge_tenant("same", "a")][0]["name"] = "重命名资料"
    context.settings.dify_console_api_key = "rotated"
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        conn.execute("UPDATE environment_plans SET expires_at=0")
    newer, _ = workspace_job(fixture, workspace_upstream)
    fixture[3].dify_console_api_key = "rotated"
    assert prepare_http(fixture, newer).json()["state"] == "observed"
    result = execute(knowledge, submit(knowledge, newer))
    assert result["state"] == "observed" and result["result"]["knowledge_base_id"] == old
    assert len([c for c in calls if c[:2] == ("POST", "/kb/knowledge-bases")]) == 1


def test_unknown_creation_zero_then_unique_reconciles_without_recreation(knowledge):
    _, _, calls, libraries, lose, _ = knowledge
    lose[0] = True
    job = submit(knowledge)
    assert execute(knowledge, job)["state"] == "reconcile"
    rows = libraries.pop(knowledge_tenant("same", "a"))
    assert execute(knowledge, job)["state"] == "reconcile"
    libraries[knowledge_tenant("same", "a")] = rows
    assert execute(knowledge, job)["state"] == "observed"
    assert len([c for c in calls if c[:2] == ("POST", "/kb/knowledge-bases")]) == 1


def test_verified_legacy_account_is_not_reused_as_environment_scope(knowledge):
    _, _, calls, _, _, context = knowledge
    context.store.record_ragflow_tenant(
        "same",
        "a",
        ragflow_email=derive_email("same"),
        credential_ciphertext=encrypt_credential("fixture", context.settings.ragflow_credential_key),
        model_checked=True,
    )
    result = execute(knowledge, submit(knowledge))
    assert result["state"] == "blocked" and result["error_code"] == "SETUP_KNOWLEDGE_SCOPE_MISMATCH"
    assert calls == []


def test_unknown_cancel_keeps_reconciling_and_ambiguous_library_blocks(knowledge):
    fixture, _, calls, libraries, lose, _ = knowledge
    lose[0] = True
    job = submit(knowledge)
    assert execute(knowledge, job)["state"] == "reconcile"
    rows = libraries.pop(knowledge_tenant("same", "a"))
    tasks.inspect(
        fixture[0], ("control", "platform", "admin"), identifier=job["id"], cancel=True, reference=fixture[1]
    )
    assert execute(knowledge, job)["state"] == "reconcile"
    libraries[knowledge_tenant("same", "a")] = rows
    assert execute(knowledge, job)["state"] == "cancelled"
    assert len([c for c in calls if c[:2] == ("POST", "/kb/knowledge-bases")]) == 1


def test_changed_shared_configuration_and_forged_resource_result_are_refused(knowledge):
    fixture, _, calls, _, _, context = knowledge
    job = submit(knowledge)
    tasks.worker(fixture[0], fixture[1], {"id": job["id"], "owner": "c" * 32})
    with pytest.raises(tasks.TaskRefused, match="RESULT_INVALID"):
        tasks.worker(
            fixture[0],
            fixture[1],
            {"id": job["id"], "owner": "c" * 32, "result": {"knowledge_base_id": "forged"}},
        )
    context.settings.ragflow_embedding_model = "changed"
    result = execute(knowledge, job)
    assert result["state"] == "blocked" and result["error_code"] == "SETUP_KNOWLEDGE_CONFIG_CHANGED"
    assert calls == []
