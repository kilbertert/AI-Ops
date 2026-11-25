"""D35：原管理入口/客服回调、临时应用生命周期及正式准入保持。"""

import time
import uuid
from types import SimpleNamespace

import pytest
import yaml
from consumer.test_candidate_trial import caller, scope
from consumer.test_consumer_agent_flow import candidate
from execution.test_app_runtime_dify_entry import HEADERS, dify_chat

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
from aiops_diagnostics.execution.chat_data_grant import DataGrantStore
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.identity.caller_auth import RegisteredCallerResolver
from aiops_diagnostics.onboarding import candidate_trial as target
from aiops_diagnostics.onboarding import consumer_trial as control
from aiops_diagnostics.onboarding import registry as environment_registry


@pytest.mark.parametrize(
    "failure", ["none", "model", "cleanup", "lost-create", "revoked", "no-tools", "observation-timeout"]
)
def test_control_target_trial_and_callback_do_not_open_normal_admission(tmp_path, monkeypatch, failure):
    import httpx

    from aiops_diagnostics.consumer import consumer_trial
    from aiops_diagnostics.dify.self_service_prepare import IMPORT_NAMESPACE

    (tmp_path / "control").mkdir()
    (tmp_path / "target").mkdir()
    with (
        dify_chat(tmp_path / "control", monkeypatch, stub_base="http://127.0.0.1:1") as central,
        dify_chat(tmp_path / "target", monkeypatch, stub_base="http://127.0.0.1:1") as remote,
    ):
        central["client"] = httpx.Client(base_url=central["gateway_base"])
        remote["client"] = httpx.Client(base_url=remote["gateway_base"])
        ctx, target_ctx = central["app"].state.gateway, remote["app"].state.gateway
        ctx.settings.environment_task_key = target_ctx.settings.environment_task_key = "a" * 40
        ctx.settings.environment_management_registry_file = "synthetic-control"
        target_ctx.settings.environment_registry_file = "synthetic-disabled"
        target_ctx.settings.consumer_chatflow_enabled = False
        current, reads, created, deleted = {"caller": caller()}, [], [], []
        monkeypatch.setattr(target, "consumer_identity", lambda *_: current["caller"])
        monkeypatch.setattr(target, "installed", lambda *_: ("https://cpms.example", ("fixed",)))
        monkeypatch.setattr(target_ctx.runtime, "_require_chat_order_visible", lambda *_: None)
        monkeypatch.setattr(
            "aiops_diagnostics.data.charging_status.read_charging_status",
            lambda base, order, **kw: reads.append((order, kw)) or {"order_no": order, "status_code": 3},
        )
        graph = candidate()
        source = SimpleNamespace()
        from aiops_diagnostics.dify.dify_dsl_pull import DifyDslSource

        source = DifyDslSource("https://console.example", "app", "synthetic-console", "workspace")
        dsl = yaml.safe_dump({"kind": "app", "version": "0.7.0", "workflow": graph})
        snapshot = (
            {"gateway_url": "https://target.example"},
            {},
            source,
            dsl,
            ("124", "same", "workspace", "kb-own"),
        )
        monkeypatch.setattr(control, "observed", lambda *_: snapshot)
        descriptor = {**scope(), "knowledge_base_id": "kb-own"}
        tokens = []

        def send(spec, **kwargs):
            assert spec.url == "https://target.example/v1/internal/consumer-data/agent"
            response = remote["client"].post(
                "/v1/internal/consumer-data/agent", headers=spec.headers, content=spec.body
            )
            if response.status_code != 200:
                raise ValueError("target refused")
            return response.json()

        monkeypatch.setattr(control, "request_json", send)

        class Console:
            def __init__(self, _):
                pass

            def _call(self, workspace, path, *, method, body):
                if path.endswith("/site-enable"):
                    assert body == {"enable_site": False}
                    return {}
                assert workspace == "workspace" and path.endswith("/imports") and method == "POST"
                request_id = body["request_id"]
                uuid.UUID(request_id)
                app = str(uuid.uuid5(uuid.UUID(IMPORT_NAMESPACE), workspace + ":" + request_id))
                created.append(app)
                if failure == "lost-create":
                    raise OSError("response lost")
                return {"app_id": app, "status": "completed"}

            def app_service_key(self, *_):
                return "synthetic-temporary-key"

            def delete_app(self, workspace, app):
                deleted.append(app)
                if failure == "cleanup":
                    raise OSError("cleanup failed")

        class DSL:
            def __init__(self, _):
                pass

            def publish_workflow(self):
                return True

            def published_workflow_id(self):
                return "11111111-2222-3333-4444-555555555555"

            def export_dsl(self, **_):
                return dsl

        class Engine:
            def __init__(self, _):
                pass

            def run(self, **kwargs):
                if failure == "model":
                    raise OSError("model failed")
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                token = kwargs["inputs"]["data_grant"]
                tokens.append(token)
                if failure == "no-tools":
                    return ChatflowReply(*ids, "看起来没有资料。", {})
                if failure == "observation-timeout":
                    from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError

                    def timeout(*_):
                        raise ConsoleApiError("DIFY_CONSOLE_UNAVAILABLE", "synthetic TimeoutError")

                    monkeypatch.setattr(control, "observed", timeout)
                response = central["client"].post(
                    "/v1/internal/consumer-data/agent",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "resource": "status",
                        "query": "",
                        "order_no": descriptor["order_no"],
                        "knowledge_base_ids": ["kb-own"],
                    },
                )
                if failure == "observation-timeout":
                    assert response.status_code == 503, response.text
                    assert response.json()["error"]["code"] == "CANDIDATE_OBSERVATION_UNAVAILABLE"
                    monkeypatch.setattr(control, "observed", lambda *_: snapshot)
                    return ChatflowReply(*ids, "知识检索暂时不可用。", {})
                assert response.status_code == 200, response.text
                assert response.json()["snapshot"]["status_code"] == 3
                if failure == "revoked":
                    current["caller"] = caller(user="other")
                return ChatflowReply(*ids, "已读到订单记录，原因仍需进一步核对。", {})

            def stop(self, **_):
                return True

        monkeypatch.setattr(consumer_trial, "DifyConsoleAdmin", Console)
        monkeypatch.setattr(consumer_trial, "DifyDslClient", DSL)
        monkeypatch.setattr(consumer_trial, "DifyChatflowClient", Engine)

        class Disabled:
            def require(self, *_):
                raise environment_registry.RegistryError("TENANT_ENVIRONMENT_NOT_ENABLED")

        target_ctx.caller_resolver = RegisteredCallerResolver(target_ctx.caller_resolver, Disabled())
        headers = build_internal_token_headers("a" * 40, 300, int(time.time()))
        response = remote["client"].post(
            "/v1/internal/environment-trial",
            headers=headers,
            json={"action": "consumer-open", "candidate": descriptor, "third_session": "fresh-session"},
        )
        assert response.status_code == 200, response.text
        lease_token = response.json()["token"]
        response = central["client"].post(
            "/v1/internal/environment-trial",
            headers=headers,
            json={
                "action": "consumer-start",
                "candidate": descriptor,
                "token": lease_token,
                "question": "查选定订单状态",
            },
        )
        assert response.status_code == 200, response.text
        trial_id = response.json()["trial_id"]
        future = ctx.runtime._futures.get(trial_id)
        if future:
            future.result(timeout=15)
        trial = ctx.runtime.get_trial(caller(), trial_id)
        assert trial["status"] == ("completed" if failure == "none" else "failed")
        assert len(created) == len(deleted) == 1 and created == deleted
        callback = {
            "candidate": descriptor,
            "action": "read",
            "resource": "status",
            "order_no": descriptor["order_no"],
        }
        assert (
            remote["client"]
            .post(
                "/v1/internal/consumer-data/agent",
                headers={"Authorization": "Bearer " + lease_token},
                json=callback,
            )
            .status_code
            == 403
        )
        assert (
            remote["client"]
            .post(
                "/v1/internal/consumer-data/status",
                headers={"Authorization": "Bearer " + lease_token},
                json={},
            )
            .status_code
            == 403
        )
        assert (
            remote["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "查询订单"})
            .status_code
            == 403
        )
        for token in tokens:
            assert DataGrantStore(ctx.store.path).by_token(token).state == "revoked"
        final = central["client"].post(
            "/v1/internal/environment-trial",
            headers=headers,
            json={"action": "consumer-read", "trial_id": trial_id},
        )
        assert final.status_code == (403 if failure == "revoked" else 200), final.text
        assert not ctx.dify_run_store.has_succeeded_for_tenant("same")
        with gateway_db.connection(ctx.store.path) as db:
            assert db.execute("SELECT count(*) FROM consumer_knowledge_grants").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM chat_route_bindings").fetchone()[0] == 0
        if failure == "none":
            for action in ("consumer-cancel", "consumer-read"):
                response = central["client"].post(
                    "/v1/internal/environment-trial",
                    headers=build_internal_token_headers("a" * 40, 300, int(time.time())),
                    json={"action": action, "trial_id": trial_id},
                )
                assert response.status_code == 403
                assert "result" not in response.json()


def test_anonymous_and_remote_management_cannot_open_a_candidate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        settings = env["settings"]
        settings.environment_task_key = "a" * 40
        settings.environment_registry_file = "synthetic-disabled"
        payload = {"action": "consumer-open", "candidate": scope(), "third_session": "fresh"}
        assert env["client"].post("/v1/internal/environment-trial", json=payload).status_code == 401
        with TestClient(env["app"], client=("203.0.113.10", 123)) as client:
            assert (
                client.post(
                    "/v1/internal/environment-trial",
                    json=payload,
                    headers=build_internal_token_headers("a" * 40, 300, int(time.time())),
                ).status_code
                == 401
            )
