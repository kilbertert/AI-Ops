"""V2.1 自助开通异步任务队列、双端路由播种与 dev-host 真机 Worker 测试（ADR-0358）。

拦截的具体错误：
1. `wizard_jobs` 允许密码/口令/私钥/token 落盘，或进度/结果回传夹带敏感键；
2. 控制台桥 `_WIZARD_ACTIONS` 与控制面 `SUPPORTED_WIZARD_ACTIONS` 不一致导致 Step 5 被桥 400 拦死；
3. 目标端 `seed_target_gateway_db` 与 `_build_environment_registry_doc` 产物通不过真实
   `EnvironmentDirectory` 与 `chat_execution.select_binding` 契约校验；
4. `wizard_worker` 子步失败后继续往下执行，未做到任一步失败即停并标失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dify_console_bridge.self_service_bridge import _WIZARD_ACTIONS as BRIDGE_WIZARD_ACTIONS
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.onboarding import (
    management,
    registry,
    wizard_dispatch,
    wizard_jobs,
    wizard_provision,
    wizard_worker,
)


def _setup_registry(tmp_path: Path) -> Path:
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir(mode=0o700)
    reg_path = reg_dir / "registry.json"
    registry.apply(
        reg_path,
        [
            {
                "id": "41",
                "name": "核心环境41",
                "enabled": True,
                "gateway_url": "https://gw41.example",
                "cpms_base_url": "https://cpms41.example",
                "company_gateway_url": "https://api41.example",
                "config_ref": str(tmp_path / "env-41.env"),
                "tenants": [],
            }
        ],
        expected_revision=0,
        actor="test",
        managers=[
            {"environment": "41", "tenant_id": "t-admin", "user_id": "u-admin", "environments": ["*"]},
        ],
    )
    return reg_path


def _fake_keygen(priv_path: Path, comment: str) -> tuple[str, str]:
    pub = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyForV21WorkerTests1234567890 {comment}"
    return "-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n", pub


def test_bridge_and_dispatch_wizard_action_parity() -> None:
    assert BRIDGE_WIZARD_ACTIONS == wizard_dispatch.SUPPORTED_WIZARD_ACTIONS
    assert len(BRIDGE_WIZARD_ACTIONS) == 14
    assert "publish_environment" in BRIDGE_WIZARD_ACTIONS


def test_wizard_jobs_lifecycle_and_secret_rejection(tmp_path: Path) -> None:
    reg_path = _setup_registry(tmp_path)
    with pytest.raises(wizard_jobs.WizardJobError, match="ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN"):
        wizard_jobs.enqueue_job(
            reg_path,
            environment="jiejia",
            action="infra_provision",
            request={"environment": "jiejia", "mysql_password": "secret-leak"},
        )

    job1 = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="infra_provision",
        request={"environment": "jiejia", "api_domain": "api.juxichongdian.cn"},
    )
    assert job1["state"] == "queued"
    assert len(job1["steps"]) == 5

    # 幂等复用尚未终态的同类任务
    job_dup = wizard_jobs.enqueue_job(
        reg_path,
        environment="jiejia",
        action="infra_provision",
        request={"environment": "jiejia", "api_domain": "api.juxichongdian.cn"},
    )
    assert job_dup["job_id"] == job1["job_id"]

    claimed = wizard_jobs.claim_next_job(reg_path, lease_seconds=120.0)
    assert claimed is not None and claimed["job_id"] == job1["job_id"]
    assert claimed["state"] == "running"

    with pytest.raises(wizard_jobs.WizardJobError, match="ENVIRONMENT_WIZARD_SECRET_PERSISTENCE_FORBIDDEN"):
        wizard_jobs.update_job_progress(
            reg_path,
            job1["job_id"],
            step="allowlist",
            step_state="passed",
            result_patch={"operator_token": "leak"},
        )

    progressed = wizard_jobs.update_job_progress(
        reg_path,
        job1["job_id"],
        step="cold_install_and_onboard",
        step_state="passed",
        result_patch={"consumer_kb_id": "kb_c_1", "operator_kb_id": "kb_o_1"},
    )
    assert progressed["consumer_kb_id"] == "kb_c_1"
    assert progressed["operator_kb_id"] == "kb_o_1"

    done = wizard_jobs.complete_job(
        reg_path,
        job1["job_id"],
        passed=True,
        result={"consumer_kb_id": "kb_c_1", "operator_kb_id": "kb_o_1"},
    )
    assert done["state"] == "completed"
    assert done["passed"] is True
    assert done["consumer_kb_id"] == "kb_c_1"

    latest = wizard_jobs.get_latest_job(reg_path, environment="jiejia", action="infra_provision")
    assert latest is not None and latest["state"] == "completed"


def test_async_infra_and_publish_dispatch_without_hooks(tmp_path: Path, monkeypatch) -> None:
    reg_path = _setup_registry(tmp_path)
    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment_registry_file=str(reg_path),
            environment="41",
            execution_environment="41",
            environment_task_key="test-internal-key",
            environment_onboarding_file="",
        ),
        wizard_hooks=wizard_dispatch.WizardRuntimeHooks(),
    )
    app = FastAPI()

    @app.exception_handler(auth.SetupRefused)
    def _refused(_req, exc: auth.SetupRefused):
        return JSONResponse({"error": {"code": str(exc)}}, status_code=400)

    @app.exception_handler(SetupConflict)
    def _conflict(_req, exc: SetupConflict):
        return JSONResponse({"error": {"code": str(exc)}}, status_code=409)

    def _session(x_aiops_console_session: str = Header(default="")) -> SimpleNamespace:
        return SimpleNamespace(access_token=x_aiops_console_session)

    monkeypatch.setattr(
        management,
        "_access",
        lambda _ctx, _sess: (SimpleNamespace(user_id="u-admin", tenant_id="t-admin"), {}, {"jiejia"}),
    )
    management.register(app, context, _session)
    client = TestClient(app)

    # 先通过引导钩子注入完成主机登记
    context.wizard_hooks = wizard_dispatch.WizardRuntimeHooks(
        host_keygen_runner=_fake_keygen,
        host_bootstrap_runner=lambda **_kw: {
            "python_minor": "3.12",
            "host_public_key_line": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostPubKeyTest",
            "host_fingerprint_sha256": "SHA256:" + "A" * 43,
        },
    )
    enr = client.post(
        "/v1/dify/management/environment-wizard",
        json={
            "wizard_action": "host_enroll_password",
            "wizard_payload": {
                "environment": "jiejia",
                "address": "8.148.223.139",
                "password": "transient-root-pw",
            },
        },
    )
    assert enr.status_code == 200, enr.text

    # 重置 infra hooks，验证生产分支自动入队 wizard_jobs 并经 infra_status 轮询可见
    context.wizard_hooks = wizard_dispatch.WizardRuntimeHooks()
    infra_res = client.post(
        "/v1/dify/management/environment-wizard",
        json={
            "wizard_action": "infra_provision",
            "wizard_payload": {
                "environment": "jiejia",
                "egress_ip": "8.148.223.139",
                "api_domain": "api.juxichongdian.cn",
                "gateway_url": "https://api.juxichongdian.cn",
                "tenant_id": "1990990768688599040",
            },
        },
    )
    assert infra_res.status_code == 200, infra_res.text
    infra_body = infra_res.json()
    assert infra_body["state"] == "queued"
    assert infra_body["job_id"].startswith("wizjob_")

    status_res = client.post(
        "/v1/dify/management/environment-wizard",
        json={"wizard_action": "infra_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert status_res.status_code == 200
    assert status_res.json()["job_id"] == infra_body["job_id"]

    # Step 5 publish_environment 入队与 worker progress/complete 回调
    pub_res = client.post(
        "/v1/dify/management/environment-wizard",
        json={
            "wizard_action": "publish_environment",
            "wizard_payload": {"environment": "jiejia", "tenant_id": "1990990768688599040"},
        },
    )
    assert pub_res.status_code == 200, pub_res.text
    pub_body = pub_res.json()
    assert pub_body["action"] == "publish_environment"
    assert pub_body["state"] == "queued"


def test_seed_target_gateway_db_and_env_directory_roundtrip(tmp_path: Path) -> None:
    env = "jiejia"
    tenant_id = "1990990768688599040"
    db_file = tmp_path / "gateway" / "gateway.db"
    key_dir = tmp_path / "dify-keys"
    key_dir.mkdir(mode=0o700)
    con_app_id = "ad90e791-92c6-5a2c-8d26-428ec93194e4"
    op_app_id = "d76a5bd5-5ed2-567e-ba3b-b80ad18e7dd5"
    con_key = key_dir / f"{con_app_id}.key"
    con_key.write_text("app-test-secret-key\n", encoding="utf-8")
    con_key.chmod(0o600)

    agent_runs_file = tmp_path / "agent-runs.json"
    agent_runs_file.write_text(
        json.dumps(
            {
                "environment": env,
                "snail_base_url": "https://api.mall.qushiyun.com",
                "snail_app_id": "aiops-environment-jiejia",
                "snail_app_token": "SAI_test",
                "snail_agent_id": 1,
                "snail_open_id": "open1234",
                "expected_node_id": f"node-{env}",
                "nodes": [{"node_id": f"node-{env}", "environment": env, "token_sha256": "a" * 64}],
            }
        ),
        encoding="utf-8",
    )
    agent_runs_file.chmod(0o600)

    manifest_text = json.dumps(
        {
            "schema": "m3-exec-config/1",
            "name": f"operator-{env}",
            "history_policy": "none",
            "model": {
                "provider_key": "openai",
                "adapter_key": "openai-compatible",
                "model_key": "deepseek-v4-1-flash-261017",
                "endpoint": "https://model-proxy:9101",
            },
            "model_options": {"max_tokens": 4096, "temperature": 0.3, "stop_sequences": []},
            "system_messages": ["test"],
            "tools": [
                {
                    "name": "charging_status_snapshot",
                    "description": "snap",
                    "input_schema": json.dumps(
                        {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
                    ),
                }
            ],
        },
        sort_keys=True,
    )

    wizard_provision.seed_target_gateway_db(
        db_file,
        {
            "environment": env,
            "tenant_id": tenant_id,
            "workspace_id": "ws-jiejia-1",
            "operator_app_id": op_app_id,
            "consumer_app_id": con_app_id,
            "consumer_workflow_id": "8d57d206-1f35-41fc-9c56-d76218eed413",
            "consumer_kb_id": "kb-con-1",
            "operator_kb_id": "kb-op-1",
            "consumer_flags": {
                "live_order_status": True,
                "live_billing_record": True,
                "photo_enabled": False,
            },
            "operator_manifest_text": manifest_text,
            "agent_runs_path": str(agent_runs_file),
            "key_dir": str(key_dir),
            "registrations": {"gateway_url": "https://api.juxichongdian.cn"},
            "consumer_target": {
                "environment": env,
                "tenant_id": tenant_id,
                "app_id": con_app_id,
                "knowledge_base_ids": ["kb-con-1"],
                "live_order_status": True,
                "live_billing_record": True,
                "photo_enabled": False,
            },
        },
    )

    caller = SimpleNamespace(effective_tenant_id=tenant_id)
    mock_settings = SimpleNamespace(
        execution_environment=env,
        chat_execution_bindings_file="",
        chat_route_bindings_file=str(db_file),
        chat_dify_key_dir=str(key_dir),
    )
    con_binding = chat_execution.select_binding(
        mock_settings, caller, scenario="consumer_service", entry="consumer"
    )
    assert con_binding is not None
    assert con_binding.app_id == con_app_id
    assert con_binding.knowledge_base_ids == ("kb-con-1",)

    op_binding = chat_execution.select_binding(
        mock_settings, caller, scenario="operator_order_diagnosis", entry="operator"
    )
    assert op_binding is not None
    assert op_binding.engine == "node"
    assert op_binding.config_file == str(agent_runs_file)

    env_dir_doc = wizard_provision._build_environment_registry_doc(
        environment=env,
        api_domain="api.juxichongdian.cn",
        tenant_id=tenant_id,
        enabled=True,
    )
    env_dir = tmp_path / "registry"
    env_dir.mkdir(mode=0o700)
    env_file = env_dir / "environment.json"
    env_file.write_text(json.dumps(env_dir_doc), encoding="utf-8")
    env_file.chmod(0o600)
    directory = registry.EnvironmentRegistry(str(env_file), env)
    assert directory.require_enabled()["id"] == env
    assert directory.enabled_tenant_ids() == frozenset({tenant_id})


def test_wizard_worker_runs_substeps_and_halts_on_failure(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    def _req(payload: dict) -> dict:
        calls.append(payload)
        if payload.get("wizard_job") == "claim":
            return {
                "job": {
                    "job_id": "wizjob_0123456789abcdef01234567",
                    "environment": "jiejia",
                    "action": "infra_provision",
                    "request": {
                        "environment": "jiejia",
                        "egress_ip": "8.148.223.139",
                        "api_domain": "api.juxichongdian.cn",
                        "gateway_url": "https://api.juxichongdian.cn",
                        "local_port": 8788,
                        "tenant_id": "1990990768688599040",
                    },
                }
            }
        return {"ok": True}

    fake_pin = SimpleNamespace(
        run_cmd=lambda *_a, **_k: SimpleNamespace(returncode=0), copy_file=lambda *_a, **_k: None
    )
    monkeypatch.setattr(wizard_worker, "_step_allowlist", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(wizard_worker, "_step_execution_node", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        wizard_worker,
        "_step_cold_install_and_onboard",
        lambda *_a, **_k: {"ok": True, "consumer_kb_id": "kb-c", "operator_kb_id": "kb-o"},
    )
    monkeypatch.setattr(
        wizard_worker,
        "_step_customer_nginx",
        lambda *_a, **_k: {"ok": False, "reason": "NGINX_SYNTAX_ERROR"},
    )
    probe_called = False

    def _never_probe(_url: str) -> dict:
        nonlocal probe_called
        probe_called = True
        return {"ok": True}

    monkeypatch.setattr(wizard_worker, "_step_live_probe", _never_probe)
    monkeypatch.setattr(
        wizard_worker.PinnedCustomerHost, "from_central", staticmethod(lambda *_a, **_k: fake_pin)
    )

    wizard_worker.run_pending_wizard_jobs(tmp_path, "41", request=_req)
    assert not probe_called
    complete_calls = [c for c in calls if c.get("wizard_job") == "complete"]
    assert len(complete_calls) == 1
    assert complete_calls[0]["passed"] is False
    assert complete_calls[0]["failed_step"] == "customer_nginx"
    assert complete_calls[0]["error_code"] == "NGINX_SYNTAX_ERROR"


def test_ephemeral_pty_runner_attaches_controlling_tty(monkeypatch) -> None:
    import fcntl
    import os
    import select
    import subprocess
    import termios

    from aiops_diagnostics.onboarding import host_enrollment

    popen_kwargs: dict[str, object] = {}
    ioctl_calls: list[tuple[int, int, int]] = []
    setsid_calls: list[bool] = []
    reads = [b"root@8.148.223.139's password: ", b"AIOPS_BOOTSTRAP_JSON:{}\r\n", b""]
    writes: list[tuple[int, bytes]] = []

    monkeypatch.setattr(host_enrollment.pty, "openpty", lambda: (101, 102))
    monkeypatch.setattr(os, "close", lambda _fd: None)
    monkeypatch.setattr(select, "select", lambda r, *_a: (r, [], []))
    monkeypatch.setattr(os, "read", lambda _fd, _n: reads.pop(0) if reads else b"")
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    monkeypatch.setattr(os, "setsid", lambda: setsid_calls.append(True))
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, arg=0: ioctl_calls.append((fd, req, arg)))

    class _FakeProc:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    def _fake_popen(_cmd, **kwargs):
        popen_kwargs.update(kwargs)
        kwargs["preexec_fn"]()
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    rc, out = host_enrollment._run_ssh_with_ephemeral_pty_password(["ssh", "root@example"], "temp-pw")
    assert rc == 0
    assert "AIOPS_BOOTSTRAP_JSON" in out
    assert "start_new_session" not in popen_kwargs
    assert callable(popen_kwargs["preexec_fn"])
    assert setsid_calls == [True]
    assert ioctl_calls == [(102, termios.TIOCSCTTY, 0)]
    assert writes == [(101, b"temp-pw\n")]


def test_prepare_control_plane_bundle_avoids_loopback_and_enables_customer_flags(
    tmp_path: Path, monkeypatch
) -> None:
    from dotenv import dotenv_values

    data_root = tmp_path / "var-lib"
    reg_dir = data_root / "environment-registry"
    reg_dir.mkdir(parents=True, mode=0o700)
    reg_path = reg_dir / "registry.json"
    registry.apply(
        reg_path,
        [
            {
                "id": "41",
                "name": "核心环境41",
                "enabled": True,
                "gateway_url": "https://gw41.example",
                "cpms_base_url": "https://cpms41.example",
                "company_gateway_url": "https://api41.example",
                "config_ref": str(tmp_path / "env-41.env"),
                "tenants": [],
            }
        ],
        expected_revision=0,
        actor="test",
        managers=[],
    )
    tpl_dir = reg_dir / "wizard-templates"
    tpl_dir.mkdir(mode=0o700)
    (tpl_dir / "jiejia.env").write_text(
        'AIOPS_MYSQL_HOST="10.0.0.32"\nAIOPS_MYSQL_PASSWORD="ro-pw"\nAIOPS_REDIS_PASSWORD="red-pw"\n',
        encoding="utf-8",
    )

    node_root = tmp_path / "node-env"
    node_jiejia = node_root / "jiejia"
    node_jiejia.mkdir(parents=True)
    (node_jiejia / "agent-runs.json").write_text(
        json.dumps({"environment": "jiejia", "nodes": []}), encoding="utf-8"
    )
    (node_jiejia / "exec-manifest.json").write_text(
        json.dumps(
            {
                "schema": "m3-exec-config/1",
                "name": "aiops-environment-jiejia",
                "history_policy": "none",
                "model": {
                    "provider_key": "openai",
                    "adapter_key": "openai-compatible",
                    "model_key": "deepseek-v4-1-flash-261017",
                    "endpoint": "https://model-proxy:9101",
                },
                "model_options": {"max_tokens": 100, "temperature": 0.1, "stop_sequences": []},
                "system_messages": ["hi"],
                "tools": [
                    {
                        "name": "mysql_readonly",
                        "description": "query db",
                        "input_schema": "{}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    op_cred = tmp_path / "op-cred.json"
    op_cred.write_text('{"api_key":"k"}', encoding="utf-8")
    con_cred = tmp_path / "con-cred.json"
    con_cred.write_text('{"api_key":"k"}', encoding="utf-8")
    dify_db = tmp_path / "gateway.db"

    class _FakeConsole:
        def __init__(self, *_a, **_k) -> None:
            pass

        def _call(self, _ws, path, **_kw):
            if "tool-providers" in path:
                return [{"id": "prov-1", "name": "cpms-node"}]
            if "/apps?" in path:
                return {
                    "data": [
                        {"id": "op-app-1", "name": "aiops-operator-jiejia", "mode": "agent-chat"},
                        {"id": "con-app-1", "name": "aiops-consumer-jiejia", "mode": "advanced-chat"},
                    ]
                }
            if path.endswith("/apps/con-app-1"):
                return {"workflow": {"id": "wf-con-1"}}
            return {"data": []}

    monkeypatch.setattr(wizard_provision, "NODE_RUNTIME_ENV_ROOT", node_root)
    monkeypatch.setattr(wizard_provision, "_ensure_kb_pair", lambda *_a, **_k: ("kb-c-1", "kb-o-1"))
    monkeypatch.setattr(
        wizard_provision.dify_ensure_workspace,
        "ensure_workspace",
        lambda **_k: SimpleNamespace(workspace_id="ws-jiejia"),
    )
    monkeypatch.setattr(wizard_provision, "DifyConsoleAdmin", _FakeConsole)
    monkeypatch.setattr(wizard_provision.knowledge_connection, "ensure", lambda *_a, **_k: None)
    monkeypatch.setattr(wizard_provision.operator_prep, "model", lambda _s: {"credential_file": str(op_cred)})
    monkeypatch.setattr(wizard_provision.self_service_prepare, "model_credentials", lambda *_a, **_k: None)
    monkeypatch.setattr(wizard_provision, "ensure_node_provider", lambda *_a, **_k: None)
    monkeypatch.setattr(
        wizard_provision,
        "_select_consumer_source",
        lambda *_a, **_k: {"workflow": {"graph": {"nodes": []}}},
    )
    monkeypatch.setattr(wizard_provision.consumer_prep, "document", lambda *_a, **_k: {})
    monkeypatch.setattr(
        wizard_provision.consumer_prep,
        "profile",
        lambda *_a, **_k: {"model": {"credential_file": str(con_cred)}},
    )
    monkeypatch.setattr(wizard_provision.consumer_prep, "dependencies", lambda *_a, **_k: None)

    def _fake_issue_key(_c, _w, app_id, d):
        p = d / f"{app_id}.key"
        p.write_text("k\n")
        return p

    monkeypatch.setattr(
        wizard_provision,
        "service_keys",
        SimpleNamespace(issue_key=_fake_issue_key),
    )

    from aiops_diagnostics.consumer import consumer_publication

    monkeypatch.setattr(
        consumer_publication,
        "flags_of",
        lambda _wf: {"live_order_status": True, "live_billing_record": True, "photo_enabled": False},
    )

    settings = SimpleNamespace(
        environment_task_key="k" * 48,
        internal_token_secret="i" * 48,
        dify_chat_base_url="http://127.0.0.1:15001",
        kb_service_base_url="http://127.0.0.1:29380",
        company_jwt_key="jwt-key-test",
        third_session_key_prefix="app:3rd_session:",
        database_file=str(dify_db),
        dify_inner_api_base_url="http://127.0.0.1:15001",
        dify_inner_api_key="inner-k",
        dify_console_base_url="http://127.0.0.1:15001",
        dify_console_api_key="console-k",
        public_base_url="https://api.mall.qushiyun.com",
        chat_dify_key_dir=str(tmp_path / "ctrl-keys"),
        execution_environment="41",
    )
    ctx = SimpleNamespace(settings=settings)
    out = wizard_provision.prepare_control_plane_bundle(
        ctx,
        reg_path,
        {
            "environment": "jiejia",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
            "tenant_name": "捷佳",
        },
    )
    rendered = dotenv_values(out["config_path"])
    assert rendered["AIOPS_GATEWAY_DIFY_CHAT_BASE_URL"] == "https://dify.ranlei.work:10443"
    assert "AIOPS_GATEWAY_KB_SERVICE_BASE_URL" not in rendered
    assert "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE" not in rendered
    assert rendered["AIOPS_GATEWAY_COMPANY_JWT_KEY"] == "jwt-key-test"
    assert rendered["AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED"] == "true"
    assert rendered["AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED"] == "true"
    assert rendered["AIOPS_MYSQL_DATABASE"] == "cloud_charging_pile"
    assert rendered["AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX"] == "app:3rd_session:"
