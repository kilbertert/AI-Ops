"""D33限定候选运行：本机管理鉴权、错主体/漂移拒绝和原准入保持。"""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.gateway import environment_trial as trial
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord


def scope(tenant="same"):
    subject = SubjectRecord(b_user_id="user", tenant_id=tenant, user_type="1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="all"),
        roles=frozenset(),
        permissions=frozenset(),
    )


@pytest.fixture
def environment(monkeypatch):
    context = SimpleNamespace(
        settings=SimpleNamespace(environment_task_key="synthetic-key", environment_registry_file="fixture"),
        operator_cpms=SimpleNamespace(),
        runtime=SimpleNamespace(_trial_operator_scope=None),
    )
    target = SimpleNamespace(cpms_base_url="https://cpms.example", proxy_base_url="https://node.example")
    binding = SimpleNamespace(environment="test", app_id="app")
    current = {"scope": scope(), "target": (target, binding, "fixed-manifest", {"tenant_id": "same"})}
    monkeypatch.setattr(trial, "target", lambda *a: current["target"])
    monkeypatch.setattr(trial, "identity", lambda *a: current["scope"])
    attempts = []

    def start(*args, **kw):
        attempts.append(kw)
        assert kw["candidate"] is True and kw["trial_manifest"] == "fixed-manifest"
        assert kw["authorize"]().effective_tenant_id == "same"
        return {"qa_id": "qa-fixture", "status": "running"}

    context.operator_cpms.start = start
    app = FastAPI()
    trial.register(app, context)
    return context, current, attempts, app


def test_candidate_request_rechecks_scope_and_manifest_without_publishing(environment):
    context, current, attempts, app = environment
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        headers = build_internal_token_headers(context.settings.environment_task_key, 300, int(time.time()))
        response = client.post(
            "/v1/internal/environment-trial",
            headers=headers,
            json={
                "action": "start",
                "app_id": "app",
                "runtime_sha256": "a" * 64,
                "company_token": "normal-test-login",
                "question": "查充电问题",
            },
        )
    assert response.status_code == 200 and response.json()["activated"] is False
    assert len(attempts) == 1
    current["scope"] = scope("other")
    with pytest.raises(trial.TrialRefused):
        attempts[0]["authorize"]()


@pytest.mark.parametrize("refusal", ["nonlocal", "missing-key", "wrong-scope", "extra", "duplicate", "array"])
def test_management_refusals_never_issue_a_run(environment, refusal):
    context, current, attempts, app = environment
    address = "203.0.113.10" if refusal == "nonlocal" else "127.0.0.1"
    headers = (
        {}
        if refusal == "missing-key"
        else build_internal_token_headers(context.settings.environment_task_key, 300, int(time.time()))
    )
    payload = dict(
        action="start", app_id="app", runtime_sha256="a" * 64, company_token="normal", question="查故障"
    )
    if refusal == "wrong-scope":
        current["scope"] = scope("other")
    if refusal == "extra":
        payload["tenant_id"] = "same"
    with TestClient(app, client=(address, 1234)) as client:
        if refusal == "duplicate":
            response = client.post(
                "/v1/internal/environment-trial",
                headers=headers,
                content='{"action":"start","action":"read"}',
            )
        elif refusal == "array":
            response = client.post("/v1/internal/environment-trial", headers=headers, json=[])
        else:
            response = client.post("/v1/internal/environment-trial", headers=headers, json=payload)
    assert response.status_code in {401, 403}
    assert attempts == []


def test_window_expires_before_tool_authorization(environment, monkeypatch):
    context, _, attempts, _ = environment
    clock = [10.0]
    monkeypatch.setattr(trial.time, "monotonic", lambda: clock[0])
    trial.start(context, dict(app_id="app", runtime_sha256="a" * 64, company_token="normal", question="查询"))
    clock[0] = 311.0
    with pytest.raises(trial.TrialRefused, match="EXPIRED"):
        attempts[0]["authorize"]()


def test_identity_must_come_from_target_user_info(monkeypatch):
    context = SimpleNamespace(
        settings=SimpleNamespace(server_config_file="fixture"),
        runtime=SimpleNamespace(_trial_operator_scope=None),
    )
    monkeypatch.setattr(
        trial.Settings,
        "from_config",
        lambda _: SimpleNamespace(upms=SimpleNamespace(base_url="https://target.example/upms")),
    )
    requests = []

    def request(spec, **kw):
        requests.append(spec)
        return dict(sysUser=dict(id="real-user", tenantId="tenant", type="1"))

    monkeypatch.setattr(trial, "request_json", request)
    caller = trial.identity(context, "normal-login-token")
    assert caller.subject.b_user_id == "real-user" and caller.effective_tenant_id == "tenant"
    assert requests[0].url == "https://target.example/upms/user/info"
    assert requests[0].follow_redirects is False


@pytest.mark.parametrize("changed", ["scope", "artifact", "expired", "upstream"])
def test_candidate_tool_roundtrip_and_completed_readback_keep_normal_admission_closed(
    tmp_path, monkeypatch, changed
):
    from operations.test_operator_agent import call_tool, finish
    from operations.test_operator_cpms import APP, HEADERS, ORDER
    from operations.test_operator_cpms import environment as operator_environment

    from aiops_diagnostics.dify import self_service_config
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.gateway import gateway_db
    from aiops_diagnostics.gateway.environment_registry import RegistryError
    from aiops_diagnostics.operator import operator_cpms

    with operator_environment(tmp_path, monkeypatch) as f:
        settings, context = f["settings"], f["ctx"]
        settings.environment_task_key = "synthetic-candidate-management"
        settings.environment_registry_file = "synthetic-disabled-directory"
        settings.operator_profiles_file = "synthetic-profile"
        profile = "operator-env-a"
        monkeypatch.setattr(
            self_service_config,
            "profiles",
            lambda _: {
                profile: {
                    "target": {
                        k: v for k, v in f["target"].__dict__.items() if k not in {"app_id", "tenant_id"}
                    }
                }
            },
        )
        SelfServiceStore(f["db"]).register(
            app_id=APP,
            tenant_id="TENANT-A",
            workspace_id="workspace",
            agent_name="candidate",
            profile_id=profile,
            actor="environment-runtime",
            environment="env-a",
        )
        with gateway_db.connection(f["db"], write=True) as db:
            db.execute("DELETE FROM chat_route_bindings")
            db.execute("DELETE FROM dify_app_registry")

        def disabled(_):
            raise RegistryError("TENANT_ENVIRONMENT_NOT_ENABLED")

        context.runtime.environment_registry = SimpleNamespace(require=disabled)
        binding = trial.chat_execution.trial_binding(settings, APP)
        current = {
            "scope": f["resolver"].current,
            "snapshot": (
                f["target"],
                binding,
                f["route"].manifest_text,
                {"tenant_id": "TENANT-A"},
                ("config", "registry"),
            ),
        }
        monkeypatch.setattr(trial, "target", lambda *a: current["snapshot"])

        def identity(*_):
            if current.get("unavailable"):
                raise trial.TrialRefused("ENVIRONMENT_TRIAL_LOGIN_REQUIRED")
            return current["scope"]

        monkeypatch.setattr(trial, "identity", identity)

        def dispatch(_, config, run):
            detail, headers, publication = call_tool(f, run, "cpms_order_detail", {"order_no": ORDER})
            assert detail["data"]["orderNo"] == ORDER
            assert publication["messages"][-1]["role"] == "user"
            finish(f, run, headers, "这笔订单的中止记录为通信中断，仍需核对设备日志。", ["cpms_order_detail"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        headers = build_internal_token_headers(settings.environment_task_key, 300, int(time.time()))
        with TestClient(f["app"], client=("127.0.0.1", 1234)) as client:
            response = client.post(
                "/v1/internal/environment-trial",
                headers=headers,
                json={
                    "action": "start",
                    "app_id": APP,
                    "runtime_sha256": "a" * 64,
                    "company_token": "operator-private",
                    "question": "查一下订单" + ORDER + "为何停止",
                },
            )
            assert response.status_code == 200, response.text
            qa_id = response.json()["trial_id"]
            run = context.agent_run_store.by_qa(qa_id)
            future = context.runtime._futures.get(qa_id)
            if future:
                future.result(timeout=10)
            assert run.run_id not in context.operator_cpms.tasks
            response = client.post(
                "/v1/internal/environment-trial",
                headers=headers,
                json={
                    "action": "read",
                    "app_id": APP,
                    "trial_id": qa_id,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "completed", response.text
            assert "通信中断" in response.json()["result"]["blocks"][0]["text"]
            assert f["client"].get("/v1/assistant/questions/" + qa_id, headers=HEADERS).status_code == 403
            with pytest.raises(operator_cpms.OperatorRefused):
                context.operator_cpms.authorize_readback(
                    current["scope"], qa_id, "operator-private", candidate_authorize=lambda: current["scope"]
                )
            if changed == "scope":
                current["scope"] = replace(
                    current["scope"], data_scope=DataScope(type="organ", site_ids=()), scope_fingerprint=""
                )
            elif changed == "artifact":
                current["snapshot"] = (*current["snapshot"][:4], ("changed", "registry"))
            elif changed == "upstream":
                current["unavailable"] = True
            else:
                authorize, bearer, _ = context.operator_cpms.candidate_authorizations[run.run_id]
                context.operator_cpms.candidate_authorizations[run.run_id] = (authorize, bearer, -1)
            refused = client.post(
                "/v1/internal/environment-trial",
                headers=headers,
                json={
                    "action": "read",
                    "app_id": APP,
                    "trial_id": qa_id,
                },
            )
            assert refused.status_code == 403 and "result" not in refused.json()
        with gateway_db.connection(f["db"]) as db:
            assert db.execute("SELECT count(*) FROM chat_route_bindings").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM dify_app_registry").fetchone()[0] == 0
