"""D27：真正的Gateway知识入口验证复合凭据与连接创建恢复。"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401
from dify.test_dify_knowledge_api import _client, _Runtime

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.dify import candidate_knowledge as candidate
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, MediaResourceSigner
from gateway.test_environment_knowledge import execute, knowledge, submit  # noqa: F401
from gateway.test_environment_tasks import prepared  # noqa: F401


@pytest.fixture
def connected(knowledge, tmp_path):  # noqa: F811
    fixture, _, _, _, _, context = knowledge
    parent = execute(knowledge, submit(knowledge))
    assert parent["state"] == "observed"
    settings = context.settings
    settings.public_base_url = "https://gateway.example"
    settings.internal_token_secret = "candidate-root-fixture"
    settings.dify_knowledge_api_key = "legacy-fixture"
    settings.kb_service_timeout_seconds = 17
    apis, calls, lose = [], [], [False]
    workspace = parent["result"]["workspace_id"]
    library = parent["result"]["knowledge_base_id"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/kb/"):
                calls.append(("KB", self.headers["tenant-id"]))
                return self.reply({"data": [{"id": library, "name": "candidate"}]})
            if self.path.startswith("/console/api/all-workspaces"):
                return self.reply({"data": [{"id": workspace, "name": "candidate"}], "has_more": False})
            assert self.headers["X-WORKSPACE-ID"] == workspace
            calls.append(("GET", self.path))
            if self.path.rsplit("/", 1)[-1] == "api-one":
                return self.reply(apis[0])
            return self.reply(
                {
                    "data": [{**r, "settings": {**r["settings"], "api_key": "[__HIDDEN__]"}} for r in apis],
                    "has_more": False,
                }
            )

        def do_POST(self):
            if self.path.startswith("/kb/"):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("SEARCH", self.path, self.headers["tenant-id"], body))
                return self.reply(
                    {
                        "data": {
                            "chunks": [
                                {
                                    "knowledge_base_id": library,
                                    "doc_id": "doc-one",
                                    "chunk_id": "chunk-one",
                                    "content": "目标环境资料",
                                    "similarity": 0.9,
                                },
                            ]
                        }
                    }
                )
            assert self.headers["X-WORKSPACE-ID"] == workspace
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not 1 <= len(body["name"]) <= 40:
                return self.reply({"error": "name must be at most 40 characters"}, 422)
            calls.append(("POST", self.path))
            apis.append({"id": "api-one", "tenant_id": workspace, **body})
            if lose[0] == "503":
                return self.reply({"error": "upstream unavailable"}, 503)
            if lose[0]:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.reply(apis[0])

        def reply(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    settings.dify_console_base_url = f"http://127.0.0.1:{server.server_port}"
    settings.kb_service_base_url = settings.dify_console_base_url
    try:
        yield fixture, parent, settings, apis, calls, lose
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def request_job(state):
    fixture, parent, *_ = state
    r = fixture[2].post(
        "/v1/dify/management/environment-tasks", json={"knowledge_task": parent["id"], "tenant_id": "same"}
    )
    assert r.status_code == 200, r.text
    return r.json()


def prepare_connection(state, job):
    fixture = state[0]
    payload = {"id": job["id"], "owner": "d" * 32, "prepare": "connection"}
    tasks.worker(fixture[0], fixture[1], {"id": job["id"], "owner": payload["owner"]})
    r = fixture[2].post(
        "/v1/internal/environment-tasks",
        json=payload,
        headers=build_internal_token_headers(fixture[3].environment_task_key, 300, int(time.time())),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_real_gateway_single_scoped_library_and_cancelled_credential(connected, tmp_path):
    fixture, parent, settings, apis, _, _ = connected
    job = request_job(connected)
    assert request_job(connected)["id"] == job["id"]
    result = prepare_connection(connected, job)
    assert result["state"] == "observed" and result["executable"] is False
    assert result["result"]["connection_id"] == "api-one"
    key = apis[0]["settings"]["api_key"]
    scope = ("a", "same", parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"])
    assert key == candidate.token(settings.internal_token_secret, scope)
    assert "api_key" not in json.dumps(result)
    runtime = _Runtime(search=lambda *args: [], signer=MediaResourceSigner("signer"))
    with _client(
        tmp_path,
        runtime,
        environment="control",
        public_base_url=settings.public_base_url,
        internal_token_secret=settings.internal_token_secret,
        environment_management_registry_file=str(fixture[0]),
        environment_onboarding_file=str(fixture[1]),
        kb_service_base_url=settings.kb_service_base_url,
        kb_service_timeout_seconds=settings.kb_service_timeout_seconds,
        dify_console_base_url=settings.dify_console_base_url,
        dify_console_api_key=settings.dify_console_api_key,
        database_file=settings.database_file,
    ) as client:
        headers = {"Authorization": "Bearer " + key}
        listed = client.get("/v1/dify/knowledge", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1
        assert listed.json()["knowledge_items"][0]["knowledge_base_ids"] == [scope[3]]
        invalid = candidate.knowledge_id(scope) + "." + "0" * 64
        assert (
            client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer " + invalid}).status_code
            == 403
        )
        forged = list(scope)
        forged[0] = "b"
        changed = candidate.knowledge_id(forged) + "." + key.rsplit(".", 1)[1]
        assert (
            client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer " + changed}).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/dify/retrieval", headers=headers, json={"knowledge_id": "kb-other", "query": "question"}
            ).status_code
            == 404
        )
        tasks.inspect(
            fixture[0],
            ("control", "platform", "admin"),
            identifier=job["id"],
            reference=fixture[1],
            cancel=True,
        )
        assert client.get("/v1/dify/knowledge", headers=headers).status_code == 403


@pytest.mark.parametrize("failure", [True, "503"])
def test_connection_response_lost_is_reconciled_without_new_post(connected, failure):
    _, _, _, _, calls, lose = connected
    lose[0] = failure
    job = request_job(connected)
    first = prepare_connection(connected, job)
    assert first["state"] == "reconcile"
    assert prepare_connection(connected, job)["state"] == "observed"
    assert len([c for c in calls if c[0] == "POST"]) == 1


def test_scoped_retrieval_media_and_revocation_during_fetch(connected, tmp_path, monkeypatch):
    from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime

    fixture, parent, settings, apis, calls, _ = connected
    job = request_job(connected)
    assert prepare_connection(connected, job)["state"] == "observed"
    scope = ("a", "same", parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"])
    signer = MediaResourceSigner("fixture-signer")
    runtime = _Runtime(search=lambda *args: [], signer=signer)
    with _client(
        tmp_path,
        runtime,
        environment="control",
        database_file=settings.database_file,
        public_base_url=settings.public_base_url,
        internal_token_secret=settings.internal_token_secret,
        environment_management_registry_file=str(fixture[0]),
        environment_onboarding_file=str(fixture[1]),
        kb_service_base_url=settings.kb_service_base_url,
        kb_service_timeout_seconds=settings.kb_service_timeout_seconds,
        dify_console_base_url=settings.dify_console_base_url,
        dify_console_api_key=settings.dify_console_api_key,
    ) as client:
        body = {
            "knowledge_id": candidate.knowledge_id(scope),
            "query": "怎样处理",
            "retrieval_setting": {"top_k": 2},
        }
        result = client.post(
            "/v1/dify/retrieval",
            headers={"Authorization": "Bearer " + apis[0]["settings"]["api_key"]},
            json=body,
        )
        assert result.status_code == 200, result.text
        assert len(result.json()["records"]) == 1
        assert result.json()["records"][0]["content"] == "目标环境资料"
        assert [c[2] for c in calls if c[0] == "SEARCH"] == [candidate.namespace(scope)]

        def cancel():
            tasks.inspect(
                fixture[0],
                ("control", "platform", "admin"),
                identifier=job["id"],
                reference=fixture[1],
                cancel=True,
            )

        media_runtime = SimpleNamespace(
            media_signer=signer, kb_search_client=object(), gateway_settings=settings, _media_proxy=None
        )
        resource = signer.issue(
            tenant_id=candidate.namespace(scope),
            agent_version=candidate.MEDIA_PREFIX + candidate.knowledge_id(scope),
            session_id=None,
            knowledge_base_id=scope[3],
            document_id="doc-one",
            chunk_id="chunk-one",
            backend_id="image-one",
            kind="image",
            mime_type="image/png",
            title="图片",
            reference_id="chunk-one",
        )
        fetched = []

        def fetch(kb, grant, **kwargs):
            assert kb.timeout == settings.kb_service_timeout_seconds
            fetched.append(kb.tenant_id)
            return b"PNGDATA"

        monkeypatch.setattr(KbServiceClient, "fetch_media", fetch)
        identifier = resource.url.rsplit("/", 1)[-1]
        assert GatewayRuntime.serve_media(media_runtime, identifier).body == b"PNGDATA"
        assert fetched == [candidate.namespace(scope)]

        def revoke(kb, grant, **kwargs):
            cancel()
            return fetch(kb, grant)

        monkeypatch.setattr(KbServiceClient, "fetch_media", revoke)
        assert GatewayRuntime.serve_media(media_runtime, identifier).status_code == 403
        before = len(fetched)
        assert GatewayRuntime.serve_media(media_runtime, identifier).status_code == 403
        assert len(fetched) == before


def test_expired_observation_stays_active_but_grant_revocation_refuses(connected):
    fixture, parent, settings, _, _, _ = connected
    job = request_job(connected)
    assert prepare_connection(connected, job)["state"] == "observed"
    scope = ("a", "same", parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"])
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET result=json_set(result,'$.expires_at',0) WHERE id=?", (job["id"],)
        )
    assert candidate.active(settings, scope)
    content = json.loads(fixture[0].read_text())
    content["managers"] = []
    fixture[0].write_text(json.dumps(content))
    with pytest.raises(candidate.CandidateKnowledgeError):
        candidate.active(settings, scope)


@pytest.mark.parametrize("presented", ["ckx_!.bad", "ckx__w.bad", "ckx_W10.bad", "ckx_" + "a" * 2200])
def test_malformed_candidate_credentials_are_rejected(presented):
    with pytest.raises(ValueError):
        candidate.parse_token("fixture", presented)


def test_workspace_lookup_outage_recovers_same_connection_task(connected, monkeypatch):
    from aiops_diagnostics.dify import environment_knowledge_connection as connection
    from aiops_diagnostics.dify.dify_ensure_workspace import EnsureError

    lookup = connection.reconcile_by_name
    job = request_job(connected)
    with monkeypatch.context() as patch:

        def unavailable(**kwargs):
            raise EnsureError("DIFY_RECONCILE_UNAVAILABLE", "synthetic outage")

        patch.setattr(connection, "reconcile_by_name", unavailable)
        assert prepare_connection(connected, job)["state"] == "reconcile"
    assert connection.reconcile_by_name is lookup
    assert prepare_connection(connected, job)["state"] == "observed"
    assert len([c for c in connected[4] if c[0] == "POST"]) == 1


def test_old_completion_cannot_restore_newer_cancelled_connection(connected):
    fixture, parent, settings, *_ = connected
    job = request_job(connected)
    assert prepare_connection(connected, job)["state"] == "observed"
    scope = ("a", "same", parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"])
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,cancel,result,updated_at) "
            "SELECT ?,environment,request,'cancelled',1,result,updated_at FROM environment_tasks WHERE id=?",
            ("task_" + "9" * 32, job["id"]),
        )
        conn.execute("UPDATE environment_tasks SET updated_at=? WHERE id=?", (time.time() + 60, job["id"]))
    with pytest.raises(candidate.CandidateKnowledgeError):
        candidate.active(settings, scope)


def test_schema_rejected_legacy_name_is_recovered_by_new_task(connected):
    import hashlib

    fixture, parent, _, apis, calls, _ = connected
    job = request_job(connected)
    scope = ("a", "same", parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"])
    old_name = "aiops-kb-" + hashlib.sha256(candidate.knowledge_id(scope).encode()).hexdigest()[:32]
    assert len(old_name) == 41
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        conn.execute(
            "UPDATE environment_tasks SET id=?,state='blocked',result=?,request=json_set(request,"
            "'$.resources',json(?)) WHERE id=?",
            (
                "task_" + "e" * 32,
                json.dumps({"expires_at": time.time() + 900}),
                json.dumps({"name": old_name, "attempted": True}),
                job["id"],
            ),
        )
    new = request_job(connected)
    assert prepare_connection(connected, new)["state"] == "observed"
    assert apis[0]["name"] == old_name[:-1] and len(apis[0]["name"]) == 40
    assert len([c for c in calls if c[0] == "POST"]) == 1
