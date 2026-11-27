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
    from aiops_diagnostics.consumer import consumer_knowledge

    validated_content = consumer_knowledge.binding_content(con_binding, caller)
    assert validated_content["schema"] == "aiops-consumer-live/1"
    assert validated_content["tenant_id"] == tenant_id

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

        def install_plugins(self, *_a, **_k) -> None:
            return None

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
                return {"workflow": {"id": "wf-con-1"}, "enable_api": True}
            if path.endswith("/apps/op-app-1"):
                return {"enable_api": True}
            return {"data": []}

    plugin_calls: list[tuple[str, str]] = []
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
    monkeypatch.setattr(wizard_provision.self_service_prepare, "consumer_models", lambda *_a, **_k: [])
    monkeypatch.setattr(wizard_provision.self_service_prepare, "consumer_plugins", lambda *_a, **_k: [])
    monkeypatch.setattr(
        wizard_provision.self_service_prepare,
        "consumer_plugin",
        lambda _c, _w, s, tok, _doc: plugin_calls.append((s.public_base_url, tok)) or "consumer_knowledge",
    )
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

    def _fake_ensure_key(_c, key_dir, _ws, app_id, *, database=None):
        _ = database
        d = Path(key_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{app_id}.key"
        p.write_text("k\n", encoding="utf-8")
        p.chmod(0o600)
        return str(p)

    monkeypatch.setattr(wizard_provision.service_keys, "ensure", _fake_ensure_key)

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
        database_file=dify_db,
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
    assert (
        rendered["AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE"]
        == "/etc/aiops-access-jiejia/runtime/knowledge-delegation.json"
    )
    assert rendered["AIOPS_GATEWAY_COMPANY_JWT_KEY"] == "jwt-key-test"
    assert rendered["AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED"] == "true"
    assert rendered["AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED"] == "true"
    assert rendered["AIOPS_MYSQL_DATABASE"] == "cloud_charging_pile"
    assert rendered["AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX"] == "app:3rd_session:"
    assert plugin_calls and plugin_calls[0][1].startswith("kdx_")

    import tarfile

    from aiops_diagnostics.knowledge import delegated_knowledge_client

    extract_dir = tmp_path / "extracted-runtime"
    extract_dir.mkdir(mode=0o700)
    with tarfile.open(out["runtime_bundle_path"], "r") as tf:
        deleg_raw = tf.extractfile("runtime/knowledge-delegation.json").read()
    deleg_path = extract_dir / "knowledge-delegation.json"
    deleg_path.write_bytes(deleg_raw)
    deleg_path.chmod(0o600)
    base_url, caps = delegated_knowledge_client.load(deleg_path, "jiejia")
    assert base_url == "https://api.mall.qushiyun.com"
    assert ("1990990768688599040", "con-app-1") in caps


def test_ephemeral_password_runner_remote_cmd_executes_under_bash_without_quote_breakout(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from aiops_diagnostics.onboarding import host_enrollment

    real_run = subprocess.run
    captured_cmds: list[str] = []

    def _fake_subproc_run(cmd, **_kwargs):
        if cmd[0] == "ssh-keyscan":
            return SimpleNamespace(
                returncode=0, stdout="8.148.223.139 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA\n"
            )
        if cmd[0] == "ssh-keygen":
            return SimpleNamespace(
                returncode=0,
                stdout="256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA root (ED25519)\n",
            )
        if cmd[0] == "ssh":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess cmd: {cmd}")

    def _fake_pty_runner(ssh_cmd: list[str], _password: str, *, timeout: float = 120.0) -> tuple[int, str]:
        _ = timeout
        remote_cmd = ssh_cmd[-1]
        captured_cmds.append(remote_cmd)
        proc = real_run(["bash", "-c", remote_cmd], capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    monkeypatch.setattr(host_enrollment.subprocess, "run", _fake_subproc_run)
    monkeypatch.setattr(host_enrollment, "_run_ssh_with_ephemeral_pty_password", _fake_pty_runner)
    monkeypatch.setattr(
        host_enrollment,
        "_build_remote_python_bootstrap",
        lambda _env, _pub: (
            'import json; print("AIOPS_BOOTSTRAP_JSON:" + '
            'json.dumps({"hostname": "jiejia-ok", "venv_ready": True}))'
        ),
    )

    priv_key = tmp_path / "id_ed25519-jiejia"
    priv_key.write_text("KEY\n", encoding="utf-8")
    facts = host_enrollment._default_ephemeral_password_runner(
        address="8.148.223.139",
        ssh_port=22,
        ssh_user="root",
        password="secret",
        public_key="ssh-ed25519 AAAAC3 test",
        private_key_path=priv_key,
        environment="jiejia",
    )
    assert facts["hostname"] == "jiejia-ok"
    assert facts["venv_ready"] is True
    assert captured_cmds and "sys.argv[1]" in captured_cmds[0]


def test_pinned_customer_host_from_central_supports_canonical_hosts_schema(monkeypatch) -> None:
    from aiops_diagnostics.onboarding import wizard_worker

    read_paths: list[str] = []

    def _fake_read(_target: str, path: str, *, cwd: Path | None = None) -> bytes:
        _ = cwd
        read_paths.append(path)
        if path.endswith("customer-hosts.json"):
            return json.dumps(
                {
                    "schema": "aiops-customer-hosts/1",
                    "hosts": {
                        "jiejia": {
                            "address": "8.148.223.139",
                            "ssh_port": 22,
                            "ssh_user": "root",
                            "key_id": "ed25519-jiejia",
                            "host_fingerprint_sha256": "SHA256:AAAA",
                            "host_public_key_line": "8.148.223.139 ssh-ed25519 AAAA",
                        }
                    },
                }
            ).encode("utf-8")
        if path.endswith("customer-host-keys/id_ed25519-jiejia"):
            return b"-----BEGIN OPENSSH PRIVATE KEY-----\nTEST\n-----END OPENSSH PRIVATE KEY-----\n"
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(wizard_worker, "_dev_host_read", _fake_read)
    pinned = wizard_worker.PinnedCustomerHost.from_central("41", "jiejia")
    assert pinned.host_ip == "8.148.223.139"
    assert pinned.expected_fingerprint == "SHA256:AAAA"
    assert any(p.endswith("/id_ed25519-jiejia") for p in read_paths)


def test_step_customer_nginx_renders_canonical_tool_ingress_and_resolves_nginx_bin() -> None:
    from aiops_diagnostics.onboarding import wizard_worker

    repo_root = Path(__file__).resolve().parents[2]
    copied: dict[str, str] = {}
    commands: list[str] = []

    class _FakePinnedHost:
        def copy_file(self, src: Path, dst: str) -> None:
            copied[dst] = src.read_text(encoding="utf-8")

        def run_cmd(self, cmd: str, **_kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = wizard_worker._step_customer_nginx(
        repo_root,
        {
            "environment": "jiejia",
            "api_domain": "api.juxichongdian.cn",
            "local_port": 8788,
            "allow_sources": ["10.0.0.9", "36.156.159.175"],
        },
        pinned_host=_FakePinnedHost(),
    )
    assert res == {"ok": True}
    tool_conf = copied["/tmp/wizard-jiejia-aiops-tool-ingress.conf"]
    assert "location = /v1/internal/consumer-knowledge" in tool_conf
    assert "/v1/internal/operator-runs/" in tool_conf
    assert "36[.]156[.]159[.]175" in tool_conf
    assert "47[.]97[.]160[.]153" in tool_conf
    assert "10[.]0[.]0[.]9" in tool_conf
    assert "120[.]55[.]45[.]59" not in tool_conf
    assert "124[.]243[.]178[.]156" not in tool_conf
    assert commands and "NGINX_BIN=" in commands[0] and "/www/server/nginx/sbin/nginx" in commands[0]


def test_step_allowlist_renders_execution_ingress_with_out_and_preserves_modes(monkeypatch) -> None:
    from aiops_diagnostics.onboarding import wizard_worker

    repo_root = Path(__file__).resolve().parents[2]
    copied: dict[str, str] = {}
    exec_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        wizard_worker,
        "_dev_host_read",
        lambda *_a, **_k: b'if ($realip_remote_addr !~ "^(124[.]243[.]178[.]156)$") { return 403; }\n',
    )

    def _fake_cp(_target: str, local_file: Path, remote_dest: str, **_kwargs) -> None:
        copied[remote_dest] = local_file.read_text(encoding="utf-8")

    def _fake_exec(target: str, _cmd: str, *, input_text: str | None = None, **_kwargs):
        exec_calls.append((target, input_text or ""))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wizard_worker, "_dev_host_cp", _fake_cp)
    res = wizard_worker._step_allowlist(repo_root, "41", "8.148.223.139", exec_fn=_fake_exec)
    assert res == {"ok": True}
    rendered = copied["/tmp/wizard-aiops-execution-ingress.conf"]
    assert "124[.]243[.]178[.]156|8[.]148[.]223[.]139" in rendered
    assert any(target == "36" and "chmod(0o644)" in script for target, script in exec_calls)
    assert any(target == "41" and "chmod(0o600)" in script for target, script in exec_calls)


def test_execute_infra_provision_job_completes_failed_when_allowlist_raises(
    tmp_path: Path, monkeypatch
) -> None:
    from aiops_diagnostics.onboarding import wizard_worker

    events: list[dict[str, object]] = []

    def _fake_request(payload: dict[str, object]) -> dict[str, object]:
        events.append(payload)
        return {"ok": True}

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(wizard_worker, "_step_allowlist", _boom)
    job = {
        "job_id": "wizjob_0123456789abcdef01234567",
        "environment": "jiejia",
        "request": {
            "environment": "jiejia",
            "egress_ip": "8.148.223.139",
            "api_domain": "api.juxichongdian.cn",
            "gateway_url": "https://api.juxichongdian.cn",
        },
    }
    res = wizard_worker.execute_infra_provision_job(
        tmp_path,
        "41",
        job,
        request=_fake_request,
        pinned_factory=lambda *_a, **_k: SimpleNamespace(),
    )
    assert res == {"ok": True}
    complete_evt = events[-1]
    assert complete_evt["wizard_job"] == "complete"
    assert complete_evt["passed"] is False
    assert complete_evt["failed_step"] == "allowlist"
    assert complete_evt["error_code"] == "ALLOWLIST_EXECUTION_ERROR"


def test_prepare_control_plane_bundle_passes_uuid_request_ids_when_creating_apps(
    tmp_path: Path, monkeypatch
) -> None:
    import re
    import types

    from aiops_diagnostics.consumer import consumer_publication
    from aiops_diagnostics.onboarding import registry, wizard_provision

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
    cred = tmp_path / "cred.json"
    cred.write_text('{"api_key":"k"}', encoding="utf-8")

    class _FakeConsole:
        def __init__(self, *_a, **_k) -> None:
            self.api_enabled_calls: list[str] = []

        def install_plugins(self, *_a, **_k) -> None:
            return None

        def _call(self, _ws, path, **kw):
            if "tool-providers" in path:
                return [{"id": "prov-1", "name": "cpms-node"}]
            if "/apps?" in path:
                return {"data": []}
            if path.endswith("/api-enable") and kw.get("method") == "POST":
                self.api_enabled_calls.append(path)
                return {"enable_api": True}
            if "/apps/" in path and not path.endswith("/draft"):
                return {"workflow": {"id": "wf-con-1"}, "enable_api": False}
            return {"data": []}

    seen_job_ids: list[str] = []

    def _fake_create_app(_c, _w, job_id, *_a, **_k):
        seen_job_ids.append(job_id)
        return "op-created"

    def _fake_copy_consumer(_c, _s, _w, job_id, *_a, **_k):
        seen_job_ids.append(job_id)
        return "con-created"

    def _fake_ensure_key(_c, key_dir, _ws, app_id, *, database=None):
        _ = database
        d = Path(key_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{app_id}.key"
        p.write_text("app-secret-token\n", encoding="utf-8")
        p.chmod(0o600)
        return str(p)

    fake_console = _FakeConsole()
    monkeypatch.setattr(wizard_provision, "NODE_RUNTIME_ENV_ROOT", node_root)
    monkeypatch.setattr(wizard_provision, "_ensure_kb_pair", lambda *_a, **_k: ("kb-c-1", "kb-o-1"))
    monkeypatch.setattr(
        wizard_provision.dify_ensure_workspace,
        "ensure_workspace",
        lambda **_k: SimpleNamespace(workspace_id="ws-jiejia"),
    )
    monkeypatch.setattr(wizard_provision, "DifyConsoleAdmin", lambda *_a, **_k: fake_console)
    monkeypatch.setattr(wizard_provision.knowledge_connection, "ensure", lambda *_a, **_k: None)
    monkeypatch.setattr(wizard_provision.operator_prep, "model", lambda _s: {"credential_file": str(cred)})
    monkeypatch.setattr(wizard_provision.self_service_prepare, "model_credentials", lambda *_a, **_k: None)
    monkeypatch.setattr(wizard_provision.self_service_prepare, "consumer_models", lambda *_a, **_k: [])
    monkeypatch.setattr(wizard_provision.self_service_prepare, "consumer_plugins", lambda *_a, **_k: [])
    monkeypatch.setattr(
        wizard_provision.self_service_prepare, "consumer_plugin", lambda *_a, **_k: "consumer_knowledge"
    )
    monkeypatch.setattr(wizard_provision.self_service_prepare, "create_app", _fake_create_app)
    monkeypatch.setattr(wizard_provision.self_service_prepare, "copy_consumer_app", _fake_copy_consumer)
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
        lambda *_a, **_k: {"model": {"credential_file": str(cred)}},
    )
    monkeypatch.setattr(wizard_provision.service_keys, "ensure", _fake_ensure_key)
    monkeypatch.setattr(
        consumer_publication,
        "flags_of",
        lambda _wf: {"live_order_status": True, "live_billing_record": True, "photo_enabled": False},
    )

    settings = types.SimpleNamespace(
        environment_task_key="k" * 48,
        internal_token_secret="i" * 48,
        dify_chat_base_url="http://127.0.0.1:15001",
        kb_service_base_url="http://127.0.0.1:29380",
        company_jwt_key="jwt-key-test",
        third_session_key_prefix="app:3rd_session:",
        database_file=tmp_path / "gateway.db",
        dify_inner_api_base_url="http://127.0.0.1:15001",
        dify_inner_api_key="inner-k",
        dify_console_base_url="http://127.0.0.1:15001",
        dify_console_api_key="console-k",
        public_base_url="https://api.mall.qushiyun.com",
        chat_dify_key_dir=str(tmp_path / "ctrl-keys"),
        execution_environment="41",
    )
    out = wizard_provision.prepare_control_plane_bundle(
        SimpleNamespace(settings=settings),
        str(reg_path),
        {
            "environment": "jiejia",
            "api_domain": "api.juxichongdian.cn",
            "tenant_id": "1990990768688599040",
            "tenant_name": "捷佳",
        },
    )
    assert out["ok"] is True
    assert len(seen_job_ids) == 2
    assert all(re.fullmatch(r"[a-f0-9-]{36}", jid) for jid in seen_job_ids)
    assert len(fake_console.api_enabled_calls) == 2

    import tarfile

    from dotenv import dotenv_values

    tpl_vals = dotenv_values(out["config_path"], interpolate=False)
    boot_vals = dotenv_values(out["bootstrap_config_path"], interpolate=False)
    for forbidden in (
        "AIOPS_GATEWAY_ENVIRONMENT",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
        "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE",
        "AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR",
    ):
        assert forbidden not in tpl_vals
        assert forbidden not in boot_vals
    for ptr in (
        "AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE",
        "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE",
        "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE",
    ):
        assert ptr in tpl_vals
        assert ptr not in boot_vals
    assert tpl_vals["AIOPS_UPMS_BASE_URL"] == "https://api.juxichongdian.cn/upms"
    assert tpl_vals["AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL"] == "https://api.juxichongdian.cn/charging-pile"

    with tarfile.open(out["runtime_bundle_path"], "r") as tf:
        op_manifest = json.loads(tf.extractfile("runtime/operator-manifest.json").read().decode("utf-8"))
        seed_doc = json.loads(tf.extractfile("runtime/gateway-seed.json").read().decode("utf-8"))
    assert op_manifest["schema"] == "operator-exec-config/1"
    assert len(op_manifest["tools"]) == 11
    agent_manifest = json.loads(seed_doc["operator_manifest_text"])
    assert agent_manifest["schema"] == "operator-agent/1"
    assert len(agent_manifest["tools"]) == 11
    assert seed_doc["key_dir"] == "/var/lib/aiops-access-jiejia/keys"


def test_seed_target_gateway_db_populates_workspace_and_apps(tmp_path: Path) -> None:
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.onboarding import wizard_provision

    db = tmp_path / "target-gateway.db"
    seed = {
        "environment": "jiejia",
        "tenant_id": "1990990768688599040",
        "workspace_id": "ws-jiejia",
        "operator_app_id": "op-app-1",
        "consumer_app_id": "con-app-1",
        "consumer_workflow_id": "wf-con-1",
        "consumer_kb_id": "kb-con-1",
        "consumer_flags": {"live_order_status": True, "live_billing_record": True, "photo_enabled": False},
        "operator_manifest_text": json.dumps(
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
                "tools": [{"name": "mysql_readonly", "description": "query", "input_schema": "{}"}],
            }
        ),
        "agent_runs_path": "/etc/aiops-access-jiejia/runtime/agent-runs.json",
        "key_dir": "/var/lib/aiops-access-jiejia/dify-keys",
    }
    res = wizard_provision.seed_target_gateway_db(db, seed)
    assert res["ok"] is True
    assert DifyWorkspaces(db).tenant_for_workspace("ws-jiejia", "jiejia") == "1990990768688599040"
    con_apps = SelfServiceStore(db).apps(entry="consumer")
    assert [a["app_id"] for a in con_apps] == ["con-app-1"]


def test_seed_target_gateway_db_upgrades_legacy_consumer_snapshot_without_schema(tmp_path: Path) -> None:
    from aiops_diagnostics.consumer import consumer_knowledge
    from aiops_diagnostics.execution.chat_route_bindings import ENGINE_DIFY, ChatRouteBindingStore
    from aiops_diagnostics.onboarding import wizard_provision
    from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentStore

    env = "jiejia"
    tenant_id = "1990990768688599040"
    db = tmp_path / "target-gateway.db"
    key_dir = tmp_path / "dify-keys"
    key_dir.mkdir(mode=0o700)
    (key_dir / "con-app-1.key").write_text("app-secret-key\n", encoding="utf-8")
    (key_dir / "con-app-1.key").chmod(0o600)
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

    wf_id = "8d57d206-1f35-41fc-9c56-d76218eed413"
    agent_store = AgentStore(db)
    bindings = ChatRouteBindingStore(db)
    legacy_agt = agent_store.create(
        tenant_id,
        f"consumer-{env}",
        "legacy",
        AgentConfig(
            agent_type="customer",
            prompt="",
            knowledge_base_ids=("kb-con-1",),
            model="deepseek-v4-1-flash-261017",
            output_contract="blocks-v1",
        ),
        "wizard-worker",
    )
    legacy_ver = agent_store.publish(
        legacy_agt.agent_id,
        tenant_id,
        1,
        {
            "app_id": "con-app-1",
            "workflow_id": wf_id,
            "consumer_service": {
                "callback_version": 6,
                "photo_enabled": False,
                "content": {
                    "tenant_id": tenant_id,
                    "environment": env,
                    "state": "published",
                    "mode": "documents",
                },
                "knowledge_base_ids": ["kb-con-1"],
                "live_order_status": True,
                "live_billing_record": True,
            },
        },
        "wizard-worker",
    )
    bindings.publish(
        environment=env,
        tenant_id=tenant_id,
        entry="consumer",
        scenario="consumer_service",
        engine=ENGINE_DIFY,
        agent_id=legacy_agt.agent_id,
        agent_version_no=legacy_ver.version_no,
        app_id="con-app-1",
        workflow_id=wf_id,
        workflow_key_ref=str(key_dir / "con-app-1.key"),
        expected_revision=0,
        updated_by="wizard-worker",
    )

    seed = {
        "environment": env,
        "tenant_id": tenant_id,
        "workspace_id": "ws-jiejia",
        "operator_app_id": "op-app-1",
        "consumer_app_id": "con-app-1",
        "consumer_workflow_id": wf_id,
        "consumer_kb_id": "kb-con-1",
        "consumer_flags": {"live_order_status": True, "live_billing_record": True, "photo_enabled": False},
        "operator_manifest_text": json.dumps(
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
                "model_options": {},
                "system_messages": ["hi"],
                "tools": [{"name": "mysql_readonly", "description": "query", "input_schema": "{}"}],
            }
        ),
        "agent_runs_path": str(agent_runs_file),
        "key_dir": str(key_dir),
    }
    res = wizard_provision.seed_target_gateway_db(db, seed)
    assert res["ok"] is True

    caller = SimpleNamespace(effective_tenant_id=tenant_id)
    mock_settings = SimpleNamespace(
        execution_environment=env,
        chat_execution_bindings_file="",
        chat_route_bindings_file=str(db),
        chat_dify_key_dir=str(key_dir),
    )
    con_binding = chat_execution.select_binding(
        mock_settings, caller, scenario="consumer_service", entry="consumer"
    )
    assert con_binding is not None
    assert con_binding.agent_version_no == 2
    content = consumer_knowledge.binding_content(con_binding, caller)
    assert content["schema"] == "aiops-consumer-live/1"


def test_step_execution_node_normalizes_jar_filename_perms_and_checks_running(monkeypatch) -> None:
    from aiops_diagnostics.onboarding import wizard_worker

    repo_root = Path(__file__).resolve().parents[2]
    captured_scripts: list[str] = []
    monkeypatch.setattr(wizard_worker, "_dev_host_cp", lambda *_a, **_k: None)

    def fake_exec(_target: str, _cmd: str, *, input_text: str = "", **_kwargs):
        captured_scripts.append(input_text)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = wizard_worker._step_execution_node(
        repo_root,
        "41",
        environment="jiejia",
        api_domain="api.juxichongdian.cn",
        exec_fn=fake_exec,
    )
    assert res == {"ok": True}
    assert len(captured_scripts) == 1
    script = captured_scripts[0]
    assert "rel_dir / 'node' / 'aiops-execution-node.jar'" in script
    assert "os.chown(_p, 0, 0)" in script
    assert "_p.chmod(0o755 if _p.is_dir() else 0o644)" in script
    assert "AIOPS_EXEC_MANIFEST=/etc/m2live/exec-manifest.json" in script
    assert "AIOPS_EXEC_MANIFEST={cfg_dir}" not in script
    assert "State.Status" in script
    assert "RestartCount" in script
    assert "app_row.get('token')" in script
    assert "/openapi/v1/user/register" in script
    assert "/openapi/v1/user/agent" in script
    assert "Snail-Ai-App-Id" in script
    assert "Snail-Ai-Token" in script
    assert "chatModelId" in script
    assert "Heartbeat rejected" in script


def test_step_cold_install_upgrades_existing_venv_when_installed_json_sha_differs(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib

    from aiops_diagnostics.onboarding import wizard_worker

    repo_root = Path(__file__).resolve().parents[2]
    app_tar = tmp_path / "app.tar.gz"
    inst_tar = tmp_path / "inst.tar.gz"
    app_tar.write_bytes(b"app-bytes")
    inst_tar.write_bytes(b"inst-bytes")
    app_sha = hashlib.sha256(b"app-bytes").hexdigest()
    inst_sha = hashlib.sha256(b"inst-bytes").hexdigest()
    payloads = {
        "/tmp/boot.env": b"BOOT=1\n",
        "/tmp/tpl.env": b"TPL=1\n",
        "/tmp/rt.tar": b"rt-bytes",
        "/tmp/auth.conf": b"set $k 1;\n",
    }
    shas = {k: hashlib.sha256(v).hexdigest() for k, v in payloads.items()}

    monkeypatch.setattr(
        wizard_worker,
        "_ensure_pool_artifacts",
        lambda *_a, **_k: {
            "revision": "0" * 40,
            "python_minor": "3.12",
            "app_remote": "/remote/app.tar.gz",
            "app_sha256": app_sha,
            "app_local": str(app_tar),
            "inst_remote": "/remote/inst.tar.gz",
            "inst_sha256": inst_sha,
            "inst_local": str(inst_tar),
        },
    )
    monkeypatch.setattr(wizard_worker, "_dev_host_read", lambda _t, path, **_k: payloads[path])

    cmds: list[str] = []

    class _FakeHost:
        def copy_file(self, _src: Path, _dst: str) -> None:
            return None

        def run_cmd(self, cmd: str, **_kwargs):
            cmds.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = wizard_worker._step_cold_install_and_onboard(
        repo_root,
        "41",
        lambda _req: {
            "ok": True,
            "bootstrap_config_path": "/tmp/boot.env",
            "bootstrap_config_sha256": shas["/tmp/boot.env"],
            "config_path": "/tmp/tpl.env",
            "config_sha256": shas["/tmp/tpl.env"],
            "runtime_bundle_path": "/tmp/rt.tar",
            "runtime_bundle_sha256": shas["/tmp/rt.tar"],
            "nginx_auth_path": "/tmp/auth.conf",
            "nginx_auth_sha256": shas["/tmp/auth.conf"],
        },
        {"environment": "jiejia", "api_domain": "api.juxichongdian.cn", "tenant_id": "1990990768688599040"},
        pinned_host=_FakeHost(),
    )
    assert res["ok"] is True
    install_sh = cmds[-1]
    assert "artifact_sha256" in install_sh
    assert "PIP_CONFIG_FILE" in install_sh
    assert "os.umask(0o022)" in install_sh
    assert "installed.json" in install_sh
    assert install_sh.index("systemctl stop") < install_sh.index("artifact_sha256")
    assert install_sh.index("artifact_sha256") < install_sh.index("seed_target_gateway_db")


def test_consumer_knowledge_catalogue_accepts_delegated_kdx_token(tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI

    from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.gateway import gateway_schema
    from aiops_diagnostics.gateway.routers import internal_callbacks
    from aiops_diagnostics.knowledge import knowledge_delegation

    db = tmp_path / "gateway.db"
    gateway_schema.ensure(db)
    ws = DifyWorkspaces(db)
    ws.claim("1990990768688599040", "jiejia", owner="test", lease_seconds=60)
    ws.mark_created("1990990768688599040", "jiejia", "ws-jiejia")
    KnowledgeGrants(db).grant("1990990768688599040", "jiejia", "kb-con-1", "test")
    SelfServiceStore(db).register(
        app_id="con-app-1",
        tenant_id="1990990768688599040",
        workspace_id="ws-jiejia",
        agent_name="consumer-con-app-1",
        profile_id="",
        actor="test",
        entry="consumer",
        environment="jiejia",
        targets={"knowledge_base_ids": ["kb-con-1"]},
    )
    settings = SimpleNamespace(
        database_file=db,
        internal_token_secret="s" * 48,
        kb_service_base_url="http://127.0.0.1:29380",
        kb_service_timeout_seconds=10.0,
        environment="41",
        execution_environment="41",
    )
    tok = knowledge_delegation.issue(settings, "jiejia", "1990990768688599040", "con-app-1", "kb-con-1")[
        "token"
    ]

    class _FakeKb:
        def list_knowledge_bases(self):
            return [{"knowledge_base_id": "kb-con-1", "name": "捷佳试跑 FAQ"}]

    monkeypatch.setattr(knowledge_delegation, "_client", lambda *_a, **_k: _FakeKb())
    app = FastAPI()
    ctx = SimpleNamespace(settings=settings, dify_workspaces=ws, runtime=SimpleNamespace())
    internal_callbacks.register(app, ctx, None, SimpleNamespace())
    client = TestClient(app)
    r = client.get("/v1/internal/consumer-knowledge/catalogue", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json() == {"libraries": [{"knowledge_base_id": "kb-con-1", "name": "捷佳试跑 FAQ"}]}


def test_activate_environment_registry_accepts_str_path_and_returns_int_revision(tmp_path: Path) -> None:
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
    out = wizard_provision.activate_environment_registry(
        SimpleNamespace(settings=SimpleNamespace()),
        str(reg_path),
        {
            "environment": "jiejia",
            "tenant_id": "1990990768688599040",
            "tenant_name": "捷佳",
            "customer_name": "捷佳四轮国内充电",
            "gateway_url": "https://api.juxichongdian.cn",
            "actor": "admin user!",
        },
    )
    assert out["ok"] is True
    assert out["registry_revision"] == 2
    loaded = registry.load(reg_path)
    jiejia_row = next(r for r in loaded["environments"] if r["id"] == "jiejia")
    assert jiejia_row["enabled"] is True
    assert jiejia_row["tenants"] == [{"id": "1990990768688599040", "name": "捷佳", "enabled": True}]


def test_dev_host_download_file_streams_large_binary_and_verifies_sha256(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    import subprocess

    from aiops_diagnostics.onboarding import wizard_worker

    payload = b"AIOPS_BINARY_CHUNK_" * (80 * 1024)  # ~1.52 MiB (> 1 MiB read truncation boundary)
    expected_sha = hashlib.sha256(payload).hexdigest()

    def _fake_run(cmd, *, stdout=None, **_kwargs):
        assert cmd[:5] == ["dev-host", "exec", "41", "--allow-service-exec", "--timeout"]
        assert stdout is not None
        stdout.write(payload)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    good_dest = tmp_path / "application.tar.gz"
    wizard_worker._dev_host_download_file(
        "41", "/var/lib/aiops-41/pool/application-3.12.tar.gz", good_dest, expected_sha256=expected_sha
    )
    assert good_dest.read_bytes() == payload
    assert (good_dest.stat().st_mode & 0o777) == 0o600

    # Truncated download must raise and remove corrupted file
    def _truncated_run(_cmd, *, stdout=None, **_kwargs):
        stdout.write(payload[:1048576])
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", _truncated_run)
    bad_dest = tmp_path / "truncated.tar.gz"
    with pytest.raises(wizard_worker.WizardWorkerError, match="DEV_HOST_DOWNLOAD_SHA_MISMATCH"):
        wizard_worker._dev_host_download_file(
            "41", "/var/lib/aiops-41/pool/application-3.12.tar.gz", bad_dest, expected_sha256=expected_sha
        )
    assert not bad_dest.exists()


def test_cold_install_and_onboard_runs_db_seed_as_service_user(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    import subprocess

    from aiops_diagnostics.onboarding import wizard_worker

    files_bytes: dict[str, bytes] = {
        "/pool/app.tar.gz": b"app-bytes",
        "/pool/inst.tar.gz": b"inst-bytes",
        "/bundle/bootstrap.env": b"AIOPS_BIND=127.0.0.1\n",
        "/bundle/template.env": b"AIOPS_BIND=127.0.0.1\n",
        "/bundle/runtime-bundle.tar": b"tar-bytes",
        "/bundle/nginx-user-ingress-auth.conf": b"set $x 1;\n",
    }
    hashes = {k: hashlib.sha256(v).hexdigest() for k, v in files_bytes.items()}

    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: "a" * 40 + "\n")
    monkeypatch.setattr(
        wizard_worker,
        "_ensure_pool_artifacts",
        lambda *_a, **_k: {
            "app_remote": "/pool/app.tar.gz",
            "app_sha256": hashes["/pool/app.tar.gz"],
            "inst_remote": "/pool/inst.tar.gz",
            "inst_sha256": hashes["/pool/inst.tar.gz"],
        },
    )
    monkeypatch.setattr(
        wizard_worker,
        "_dev_host_download_file",
        lambda _t, rpath, dest, **_k: dest.write_bytes(files_bytes[rpath]) or dest.chmod(0o600),
    )
    monkeypatch.setattr(wizard_worker, "_dev_host_read", lambda _t, rpath, **_k: files_bytes[rpath])

    cmds: list[str] = []

    class _Pin:
        def copy_file(self, *_a, **_k) -> None:
            return None

        def run_cmd(self, cmd: str, **_k):
            cmds.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    out = wizard_worker._step_cold_install_and_onboard(
        tmp_path,
        "41",
        lambda _p: {
            "ok": True,
            "bootstrap_config_path": "/bundle/bootstrap.env",
            "bootstrap_config_sha256": hashes["/bundle/bootstrap.env"],
            "config_path": "/bundle/template.env",
            "config_sha256": hashes["/bundle/template.env"],
            "runtime_bundle_path": "/bundle/runtime-bundle.tar",
            "runtime_bundle_sha256": hashes["/bundle/runtime-bundle.tar"],
            "nginx_auth_path": "/bundle/nginx-user-ingress-auth.conf",
            "nginx_auth_sha256": hashes["/bundle/nginx-user-ingress-auth.conf"],
        },
        {"environment": "jiejia", "api_domain": "api.juxichongdian.cn", "local_port": 8788},
        pinned_host=_Pin(),
    )
    assert out["ok"] is True
    install_sh = cmds[-1]
    assert "systemctl stop aiops-access-jiejia.service" in install_sh
    assert "chown -R aiops-access-jiejia:aiops-access-jiejia" in install_sh
    assert (
        "runuser -u aiops-access-jiejia -- /opt/aiops-access-jiejia/current/venv/bin/python -c" in install_sh
    )
    assert install_sh.index("chown -R aiops-access-jiejia:aiops-access-jiejia") < install_sh.index(
        "runuser -u aiops-access-jiejia --"
    )
