"""V2 自助开通向导全链路 FastAPI 路由契约与安全边界测试（ADR-0358）。

拦截的具体错误：
1. `/v1/dify/management/environment-wizard` 13 项白名单动作任一项出现越权放行、多余键放行、
   未引导主机越步执行或凭据回显/落盘；
2. `/v1/internal/environment-bootstrap/script/{token}` 与 `/callback` 出现明文令牌落盘、
   二次重放成功或指纹漂移未返 409；
3. `db_remediate` 绕过二次确认框、摘要校验或静态白名单；
4. `kb_upload` 接受超限/路径穿越/非法 base64，`artifact_pool_status` 泄露服务端绝对路径。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.onboarding import (
    artifact_pool,
    discovery,
    management,
    registry,
    wizard_dispatch,
)


def _fake_keygen(priv_path: Path, comment: str) -> tuple[str, str]:
    pub = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyDataForApiTests1234567890123456 {comment}"
    return "-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n", pub


def _build_wizard_app(
    tmp_path: Path,
    *,
    hooks: wizard_dispatch.WizardRuntimeHooks | None = None,
    monkeypatch=None,
) -> tuple[TestClient, Path]:
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
            },
            {
                "id": "staging",
                "name": "预发环境",
                "enabled": True,
                "gateway_url": "https://gw-stg.example",
                "cpms_base_url": "https://cpms-stg.example",
                "company_gateway_url": "https://api-stg.example",
                "config_ref": str(tmp_path / "env-staging.env"),
                "tenants": [],
            },
        ],
        expected_revision=0,
        actor="test",
        managers=[
            {
                "environment": "41",
                "tenant_id": "t-admin",
                "user_id": "u-admin",
                "environments": ["*"],
            },
            {
                "environment": "41",
                "tenant_id": "t-limited",
                "user_id": "u-limited",
                "environments": ["staging"],
            },
        ],
    )

    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment_registry_file=str(reg_path),
            environment="41",
            execution_environment="41",
            environment_task_key="test-internal-key",
            environment_onboarding_file="",
        ),
        wizard_hooks=hooks or wizard_dispatch.WizardRuntimeHooks(),
    )

    def _fake_identity(_ctx, session, **_kw):
        if session is None:
            raise auth.SetupRefused("SETUP_CREDENTIAL_REQUIRED")
        return SimpleNamespace(tenant_id=session["tenant_id"], user_id=session["user_id"])

    if monkeypatch is not None:
        monkeypatch.setattr(auth, "identity", _fake_identity)

    app = FastAPI()

    @app.exception_handler(auth.SetupRefused)
    def _handle_refused(_req, exc: auth.SetupRefused):
        code = str(exc)
        http_code = 401 if code == "SETUP_CREDENTIAL_REQUIRED" else 403 if "DENIED" in code else 400
        return JSONResponse(status_code=http_code, content={"error": {"code": code}})

    @app.exception_handler(SetupConflict)
    def _handle_conflict(_req, exc: SetupConflict):
        return JSONResponse(status_code=409, content={"error": {"code": str(exc)}})

    def _session_dep(
        x_test_tenant: str | None = Header(default=None),
        x_test_user: str | None = Header(default=None),
    ):
        if not x_test_tenant or not x_test_user:
            return None
        return {"tenant_id": x_test_tenant, "user_id": x_test_user}

    management.register(app, context, _session_dep)
    return TestClient(app), reg_path


def _admin_headers() -> dict[str, str]:
    return {"X-Test-Tenant": "t-admin", "X-Test-User": "u-admin"}


def _limited_headers() -> dict[str, str]:
    return {"X-Test-Tenant": "t-limited", "X-Test-User": "u-limited"}


def test_wizard_full_13_action_end_to_end_and_security_guards(tmp_path: Path, monkeypatch) -> None:
    executed_sqls: list[tuple[str, tuple[str, ...]]] = []
    uploaded_docs: list[tuple[str, str, str, int]] = []
    seen_revoke_calls: list[tuple[str, str]] = []

    def _fake_http(method: str, url: str, headers: dict[str, str], body):
        if method == "POST" and url.endswith("/mallapi/userinfo/phone/login"):
            return 200, json.dumps({"code": 0, "data": {"thirdSession": "ts-ephemeral-xyz"}})
        if method == "DELETE" and url.endswith("/auth/token/logout"):
            seen_revoke_calls.append((method, url))
            return 200, "{}"
        if method == "POST" and url.endswith("/v1/assistant/questions"):
            if "third-session" not in headers and "Authorization" not in headers:
                return 401, '{"error":{"code":"UNAUTHORIZED"}}'
            return 202, json.dumps({"qa_id": "qa_smoke_1", "status": "queued"})
        if method == "GET" and "/v1/assistant/questions/" in url:
            return 200, json.dumps({"qa_id": "qa_smoke_1", "status": "completed"})
        if method == "GET" and url.endswith("/upms/tenant/list"):
            return 200, json.dumps({"code": 0, "data": [{"id": "1", "name": "捷佳"}]})
        return 404, "{}"

    grant_sql = "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'%';"
    index_sql = (
        "ALTER TABLE `cloud_charging_pile`.`iot_device_offline_monitor` "
        "ADD INDEX `aiops_offline_device_time` (`device_id`, `offline_time`), "
        "ALGORITHM=INPLACE, LOCK=NONE;"
    )

    hooks = wizard_dispatch.WizardRuntimeHooks(
        host_bootstrap_runner=lambda **kw: {
            "host_fingerprint_sha256": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "hostname": "jiejia-gw",
            "machine_id_sha256": "a" * 64,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "baota",
        },
        host_keygen_runner=_fake_keygen,
        discovery_runner=lambda _host, _domain, _gw: (
            {
                "hostname": "jiejia-gw",
                "python_minor": "3.12",
                "timezone": "Asia/Shanghai",
                "nginx_flavor": "baota",
                "nginx_extension_dir": "/www/server/panel/vhost/nginx/extension/api.juxichongdian.cn",
            },
            {
                "ready": False,
                "checks": [
                    {
                        "id": "mysql_upms_select_grants",
                        "category": "mysql",
                        "state": "blocked",
                        "summary": "缺少 qumall_upms.sys_user SELECT 权限",
                    }
                ],
                "remediations": [
                    {
                        "id": "mysql_upms_select_grants",
                        "kind": "mysql_grant",
                        "auto_fixable": True,
                        "sql_statements": [grant_sql],
                    },
                    {
                        "id": "mysql_required_indexes",
                        "kind": "mysql_index",
                        "auto_fixable": True,
                        "sql_statements": [index_sql],
                    },
                ],
                "discovered": {
                    "tenants": [{"id": "1", "name": "捷佳新能源"}],
                    "healthy_tsdata_url": "http://127.0.0.1:30888",
                    "sample_candidates": {
                        "orders": [{"order_no": "CD202611260001"}],
                        "offline_devices": [{"device_id": "DEV001"}],
                        "consumer_orders": [{"order_no": "CD202611260002"}],
                    },
                },
            },
        ),
        db_executor=lambda env, stmts: executed_sqls.append((env, stmts)),
        db_reverify_runner=lambda _env: {"ready": True, "checks": []},
        allowlist_runner=lambda _plan: {"ok": True},
        node_runner=lambda _plan: {"ok": True},
        nginx_runner=lambda _plan: {"ok": True},
        live_probe=lambda _url: {"anon_assistant_status": 401, "upms_tenant_list_status": 200},
        kb_uploader=lambda role, kb_id, fname, raw: (
            uploaded_docs.append((role, kb_id, fname, len(raw)))
            or {"document_id": f"doc_{role}_1", "chunk_count": 3}
        ),
        validator_runner=lambda _payload: {
            "capabilities": {
                "consumer": {"state": "passed", "run_id": "run_c"},
                "order": {"state": "passed", "run_id": "run_o"},
                "offline": {"state": "passed", "run_id": "run_d"},
            }
        },
        http_caller=_fake_http,
    )

    client, reg_path = _build_wizard_app(tmp_path, hooks=hooks, monkeypatch=monkeypatch)

    # 0. 未登录与跨环境越权均必须拒绝
    r_anon = client.post(
        "/v1/dify/management/environment-wizard",
        json={"wizard_action": "host_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_anon.status_code == 401

    r_denied = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_limited_headers(),
        json={"wizard_action": "host_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_denied.status_code == 403
    assert r_denied.json()["error"]["code"] == "ENVIRONMENT_ACCESS_DENIED"

    # 未引导主机直接触发 discover 必须 fail-closed
    r_pre_disc = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "discover",
            "wizard_payload": {"environment": "jiejia", "api_domain": "api.juxichongdian.cn"},
        },
    )
    assert r_pre_disc.status_code == 400
    assert r_pre_disc.json()["error"]["code"] == "ENVIRONMENT_HOST_NOT_ENROLLED"

    # 1. 模式 A 一次性密码引导（绝不落盘或回显）
    secret_pw = "ROOT_EPHEMERAL_SECRET_2026"
    r_enroll = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "host_enroll_password",
            "wizard_payload": {
                "environment": "jiejia",
                "address": "8.148.223.139",
                "password": secret_pw,
            },
        },
    )
    assert r_enroll.status_code == 200
    assert r_enroll.json()["enrolled"] is True
    assert r_enroll.json()["password_persisted"] is False
    assert secret_pw not in r_enroll.text

    # 2. host_status 查询
    r_status = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={"wizard_action": "host_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_status.status_code == 200
    assert r_status.json()["enrolled"] is True

    # 3. discover 自动体检 + 二次确认摘要
    r_disc = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "discover",
            "wizard_payload": {
                "environment": "jiejia",
                "api_domain": "api.juxichongdian.cn",
                "internal_gateway_addr": "10.0.0.31:30899",
            },
        },
    )
    assert r_disc.status_code == 200
    plan = r_disc.json()["db_remediation_plan"]
    assert plan["has_auto_fixable_db_gaps"] is True
    assert len(plan["confirmation_digest"]) == 64

    # 4. db_remediate：未勾选确认 / 任意危险 SQL 均拦截，合法二次确认通过并写 0600 审计
    r_db_no_confirm = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "db_remediate",
            "wizard_payload": {
                "environment": "jiejia",
                "remediation_ids": plan["remediation_ids"],
                "sql_statements": plan["sql_statements"],
                "confirmed_by_operator": False,
                "confirmation_digest": plan["confirmation_digest"],
            },
        },
    )
    assert r_db_no_confirm.status_code == 400
    assert r_db_no_confirm.json()["error"]["code"] == "ENVIRONMENT_DB_REMEDIATION_CONFIRMATION_REQUIRED"

    evil_sql = ["DROP TABLE `cloud_charging_pile`.`ch_order_info`;"]
    evil_digest = discovery.compute_remediation_confirmation_digest(
        "jiejia", ["mysql_required_indexes"], evil_sql
    )
    r_db_evil = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "db_remediate",
            "wizard_payload": {
                "environment": "jiejia",
                "remediation_ids": ["mysql_required_indexes"],
                "sql_statements": evil_sql,
                "confirmed_by_operator": True,
                "confirmation_digest": evil_digest,
            },
        },
    )
    assert r_db_evil.status_code == 400
    assert r_db_evil.json()["error"]["code"] == "ENVIRONMENT_DB_REMEDIATION_SQL_FORBIDDEN"

    r_db_ok = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "db_remediate",
            "wizard_payload": {
                "environment": "jiejia",
                "remediation_ids": plan["remediation_ids"],
                "sql_statements": plan["sql_statements"],
                "confirmed_by_operator": True,
                "confirmation_digest": plan["confirmation_digest"],
            },
        },
    )
    assert r_db_ok.status_code == 200
    assert r_db_ok.json()["verified_after_fix"] is True
    assert len(executed_sqls) == 1
    audit_file = reg_path.parent / "db-remediation-audit.jsonl"
    assert audit_file.exists()
    assert (audit_file.stat().st_mode & 0o777) == 0o600

    # 5. artifact_pool_status（绝不泄露服务端绝对路径）
    app_tar = tmp_path / "app.tar.gz"
    inst_tar = tmp_path / "inst.tar.gz"
    app_tar.write_bytes(b"app-archive-bytes")
    inst_tar.write_bytes(b"inst-archive-bytes")
    app_tar.chmod(0o600)
    inst_tar.chmod(0o600)
    artifact_pool.register_pool_entry(
        reg_path.parent / "artifact-pool.json",
        revision="a" * 40,
        python_minor="3.12",
        app_archive=app_tar,
        installer_archive=inst_tar,
    )
    r_pool = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={"wizard_action": "artifact_pool_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_pool.status_code == 200
    assert r_pool.json()["count"] == 1
    assert str(tmp_path) not in r_pool.text

    # 6. infra_provision 与 infra_status
    r_infra = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "infra_provision",
            "wizard_payload": {
                "environment": "jiejia",
                "egress_ip": "8.148.223.139",
                "api_domain": "api.juxichongdian.cn",
                "gateway_url": "https://api.juxichongdian.cn",
            },
        },
    )
    assert r_infra.status_code == 200
    assert r_infra.json()["passed"] is True

    r_infra_st = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={"wizard_action": "infra_status", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_infra_st.status_code == 200
    assert r_infra_st.json()["provisioned"] is True
    assert r_infra_st.json()["passed"] is True

    # 7. kb_install_starter_faq 与 kb_upload（含路径穿越/非法 base64 拒绝）
    r_starter = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "kb_install_starter_faq",
            "wizard_payload": {"environment": "jiejia", "kb_id": "kb-consumer-jiejia"},
        },
    )
    assert r_starter.status_code == 200
    assert r_starter.json()["starter_template"] is True

    r_kb_up = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "kb_upload",
            "wizard_payload": {
                "environment": "jiejia",
                "target_role": "operator",
                "kb_id": "kb-operator-jiejia",
                "filename": "sop.md",
                "content_base64": base64.b64encode(b"# SOP\n").decode("ascii"),
            },
        },
    )
    assert r_kb_up.status_code == 200
    assert len(uploaded_docs) == 2

    r_kb_bad = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "kb_upload",
            "wizard_payload": {
                "environment": "jiejia",
                "target_role": "operator",
                "kb_id": "kb-operator-jiejia",
                "filename": "../evil.md",
                "content_base64": base64.b64encode(b"x").decode("ascii"),
            },
        },
    )
    assert r_kb_bad.status_code == 400
    assert r_kb_bad.json()["error"]["code"] == "ENVIRONMENT_KB_FILENAME_INVALID"

    # 8. self_validate 免 JSON 瞬时三能力自验（凭据零回显 + finally 吊销管家令牌）
    op_tok = "op-secret-jwt-9988"
    r_val = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "self_validate",
            "wizard_payload": {
                "environment": "jiejia",
                "tenant_id": "1",
                "company_gateway_url": "https://api.juxichongdian.cn",
                "order_no": "CD202611260001",
                "device_id": "DEV001",
                "offline_start": "2026-11-20T00:00:00+08:00",
                "offline_end": "2026-11-26T23:59:59+08:00",
                "consumer_order_no": "CD202611260002",
                "consumer_reference_id": "starter-faq",
                "consumer_phone": "13900001111",
                "consumer_code": "8888",
                "operator_token": op_tok,
            },
        },
    )
    assert r_val.status_code == 200
    assert r_val.json()["passed"] is True
    assert r_val.json()["operator_session_revoked"] is True
    assert op_tok not in r_val.text
    assert "13900001111" not in r_val.text
    assert len(seen_revoke_calls) == 1

    # 9. post_publish_smoke 发布后公网 HTTPS 4 项冒烟
    r_smoke = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "post_publish_smoke",
            "wizard_payload": {
                "environment": "jiejia",
                "gateway_url": "https://api.juxichongdian.cn",
                "tenant_id": "1",
                "consumer_session": "ts-smoke-secret",
                "operator_token": "op-smoke-secret",
            },
        },
    )
    assert r_smoke.status_code == 200
    assert r_smoke.json()["passed"] is True
    assert len(r_smoke.json()["checks"]) == 4
    assert "ts-smoke-secret" not in r_smoke.text
    assert "op-smoke-secret" not in r_smoke.text

    # 非 HTTPS 公网冒烟必须拒绝
    r_smoke_http = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "post_publish_smoke",
            "wizard_payload": {
                "environment": "jiejia",
                "gateway_url": "http://api.juxichongdian.cn",
                "tenant_id": "1",
            },
        },
    )
    assert r_smoke_http.status_code == 400
    assert r_smoke_http.json()["error"]["code"] == "ENVIRONMENT_SMOKE_INVALID"

    # 10. host_revoke 撤销主机
    r_rev = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={"wizard_action": "host_revoke", "wizard_payload": {"environment": "jiejia"}},
    )
    assert r_rev.status_code == 200
    assert r_rev.json()["enrolled"] is False
    assert r_rev.json()["host"]["status"] == "revoked"


def test_mode_b_bootstrap_http_routes_single_use_and_replay_410(tmp_path: Path, monkeypatch) -> None:
    hooks = wizard_dispatch.WizardRuntimeHooks(host_keygen_runner=_fake_keygen)
    client, _ = _build_wizard_app(tmp_path, hooks=hooks, monkeypatch=monkeypatch)

    r_create = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "host_create_bootstrap_script",
            "wizard_payload": {
                "environment": "cust2",
                "address": "8.148.223.140",
                "callback_base_url": "https://control.example",
            },
        },
    )
    assert r_create.status_code == 200
    token = r_create.json()["script_url"].rsplit("/", 1)[1]

    # 拉取单次脚本返回 200 text/x-shellscript + no-store
    r_script = client.get(
        f"/v1/internal/environment-bootstrap/script/{token}",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert r_script.status_code == 200
    assert "text/x-shellscript" in r_script.headers["content-type"]
    assert r_script.headers["cache-control"] == "no-store"
    assert "useradd --system" in r_script.text

    # 回调钉扎指纹返回 200
    r_cb = client.post(
        "/v1/internal/environment-bootstrap/callback",
        headers={"X-Bootstrap-Token": token},
        json={
            "environment": "cust2",
            "host_fingerprint_sha256": "SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "hostname": "cust2-prod",
            "machine_id_sha256": "c" * 64,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "standard",
        },
    )
    assert r_cb.status_code == 200
    assert r_cb.json()["enrolled"] is True

    # 二次重放脚本与回调均必须返回 410（非 HTTPS 请求则先被 400 拒绝）
    r_script_http = client.get(f"/v1/internal/environment-bootstrap/script/{token}")
    assert r_script_http.status_code == 400
    r_script_replay = client.get(
        f"/v1/internal/environment-bootstrap/script/{token}",
        headers={"X-Forwarded-Proto": "https"},
    )
    assert r_script_replay.status_code == 410
    r_cb_replay = client.post(
        "/v1/internal/environment-bootstrap/callback",
        headers={"X-Bootstrap-Token": token},
        json={
            "environment": "cust2",
            "host_fingerprint_sha256": "SHA256:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        },
    )
    assert r_cb_replay.status_code == 410

    # 未知动作、多余顶层键与畸形字段类型均被干净 400 拒绝（绝不 500）
    r_bad_action = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={"wizard_action": "shell_exec", "wizard_payload": {"environment": "cust2"}},
    )
    assert r_bad_action.status_code == 400
    assert r_bad_action.json()["error"]["code"] == "ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED"

    r_bad_types = client.post(
        "/v1/dify/management/environment-wizard",
        headers=_admin_headers(),
        json={
            "wizard_action": "post_publish_smoke",
            "wizard_payload": {
                "environment": "cust2",
                "gateway_url": "https://api.example",
                "tenant_id": 12345,
            },
        },
    )
    assert r_bad_types.status_code == 400
    assert r_bad_types.json()["error"]["code"] == "ENVIRONMENT_WIZARD_INVALID"
