"""V2.1 自助开通向导真实生产运行器测试（ADR-0358）。

拦截的具体错误：
1. 模式 A 口令通过 stdlib pty 递交失败、哨兵提取损坏或密码遗留在异常/返回中；
2. 已钉扎主机 SSH 指纹漂移未 fail-closed 拒绝，或跳过 known_hosts 校验；
3. ``run_live_discovery`` 未落盘 ``0600`` 模板、将 Redis/MySQL 明文密码返回前端或隧道未关闭；
4. ``run_live_db_remediation`` 缺失二次确认/管理凭据放行，或未以 ``aiops_ro`` 复验；
5. ``upload_to_kb_service`` 未带作用域租户头、multipart 字段非 ``files`` 或未触发解析；
6. ``run_live_self_validation`` 未发送标准问句或未在 ``finally`` 吊销临时管家令牌。
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.onboarding import (
    host_enrollment,
    management,
    registry,
    wizard_dispatch,
    wizard_runtime,
)


def _fake_keygen(priv_path: Path, comment: str) -> tuple[str, str]:
    pub = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyDataForV21Tests123456789012345 {comment}"
    return "-----BEGIN OPENSSH PRIVATE KEY-----\nMOCK\n-----END OPENSSH PRIVATE KEY-----\n", pub


def _setup_registry(tmp_path: Path) -> Path:
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir(mode=0o700)
    reg_path = reg_dir / "registry.json"
    registry.apply(
        reg_path,
        [
            {
                "id": "41",
                "name": "核心41",
                "enabled": True,
                "gateway_url": "https://gw41.example",
                "cpms_base_url": "https://cpms41.example",
                "company_gateway_url": "https://api41.example",
                "config_ref": str(tmp_path / "env41.env"),
                "tenants": [],
            }
        ],
        expected_revision=0,
        actor="test",
        managers=[
            {
                "environment": "41",
                "tenant_id": "t-admin",
                "user_id": "u-admin",
                "environments": ["*"],
            }
        ],
    )
    return reg_path


def _enroll_test_host(reg_path: Path, fp: str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") -> None:
    host_enrollment.enroll_via_ephemeral_password(
        reg_path,
        ("41", "t-admin", "u-admin"),
        environment="custnew",
        address="8.148.223.139",
        password="ONE_TIME_ROOT_PW_XYZ",
        bootstrap_runner=lambda **_kw: {
            "host_fingerprint_sha256": fp,
            "host_public_key_line": "8.148.223.139 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockHostKey",
            "hostname": "custnew-gw",
            "machine_id_sha256": "a" * 64,
            "python_minor": "3.12",
            "service_user_present": True,
            "venv_ready": True,
            "nginx_flavor": "baota",
        },
        keygen_runner=_fake_keygen,
    )


def _assert_eq(a, b):
    assert a == b
    return None


def test_pty_bootstrap_extracts_sentinel_and_pinned_ssh_rejects_drift(tmp_path: Path, monkeypatch) -> None:
    sample_raw = (
        "Authorized uses only.\r\n"
        + host_enrollment._BOOTSTRAP_JSON_MARKER
        + json.dumps({"hostname": "jiejia-host", "python_minor": "3.12", "nginx_flavor": "baota"})
        + "\r\n"
    )
    facts = host_enrollment._extract_bootstrap_facts(sample_raw)
    assert facts["hostname"] == "jiejia-host"
    assert facts["nginx_flavor"] == "baota"

    with pytest.raises(host_enrollment.EnrollmentRefused, match="ENVIRONMENT_HOST_PASSWORD_AUTH"):
        host_enrollment._extract_bootstrap_facts("Permission denied, please try again.")

    reg_path = _setup_registry(tmp_path)
    _enroll_test_host(reg_path, fp="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    conn_info = host_enrollment.load_enrolled_host_connection(reg_path, "custnew")
    assert conn_info["environment"] == "custnew"
    assert conn_info["host_public_key_line"].startswith("8.148.223.139 ssh-ed25519 ")

    bad_fp = "256 SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB no-comment (ED25519)\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=bad_fp, stderr=""),
    )
    with pytest.raises(host_enrollment.EnrollmentRefused, match="ENVIRONMENT_HOST_FINGERPRINT_MISMATCH"):
        host_enrollment._resolve_pinned_known_hosts_line(conn_info)


def test_live_discovery_and_db_remediation_write_0600_template_without_secret_echo(
    tmp_path: Path, monkeypatch
) -> None:
    reg_path = _setup_registry(tmp_path)
    _enroll_test_host(reg_path)
    mysql_secret = "MYSQL_RO_SECRET_9911"
    admin_secret = "MYSQL_ADMIN_SECRET_8822"
    redis_secret = "REDIS_LOCAL_SECRET_7733"
    tunnel_closed = {"closed": False}

    def fake_run_ssh(_conn, _args, *, stdin_text="", timeout=45.0):
        if "AIOPS_DATA_CLUES_JSON" in stdin_text:
            payload = {"redis_host": "127.0.0.1", "redis_port": 6379, "redis_password": redis_secret}
            return subprocess.CompletedProcess(
                [], 0, stdout="AIOPS_DATA_CLUES_JSON:" + json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "environment": "custnew",
                    "hostname": "custnew-gw",
                    "python_minor": "3.12",
                    "timezone": "Asia/Shanghai",
                    "nginx_flavor": "baota",
                    "nginx_extension_dir": "/www/server/panel/vhost/nginx/extension/api.example.cn",
                    "tsdata_loopback_candidates": ["http://127.0.0.1:30888"],
                    "tenants": [{"id": "1990990768688599040", "name": "捷佳"}],
                }
            ),
            stderr="",
        )

    @contextmanager
    def fake_tunnel(_conn, targets, **_kw):
        ports = {name: 39000 + i for i, name in enumerate(targets)}
        try:
            yield ports
        finally:
            tunnel_closed["closed"] = True

    monkeypatch.setattr(host_enrollment, "run_pinned_ssh", fake_run_ssh)
    monkeypatch.setattr(host_enrollment, "open_pinned_ssh_tunnel", fake_tunnel)

    idx_map = {
        "iot_device_offline_monitor": [
            {"Key_name": "k1", "Column_name": "device_id", "Seq_in_index": 1},
            {"Key_name": "k1", "Column_name": "offline_time", "Seq_in_index": 2},
        ],
        "ch_order_info": [{"Key_name": "k2", "Column_name": "device_id", "Seq_in_index": 1}],
    }
    sample_data = {
        "orders": [{"order_no": "CD20261126001"}],
        "offline_devices": [{"device_id": "D1"}],
        "consumer_orders": [{"order_no": "CD20261126002"}],
    }
    grant_sql = "GRANT SELECT ON `qumall_upms`.`sys_user` TO 'aiops_ro'@'%';"
    monkeypatch.setattr(
        wizard_runtime.data_preflight,
        "_default_mysql_probe",
        lambda cfg, _dbs, discover_samples=False: (
            _assert_eq(cfg["password"], mysql_secret)
            or {
                "connected": True,
                "client_host": "%",
                "table_grants": {
                    "charging": {
                        "ch_order_info": True,
                        "iot_charging_device": True,
                        "iot_device_offline_monitor": True,
                        "iot_device_failure_records": True,
                        "ch_site": True,
                    },
                    "mall": {
                        "order_info": True,
                        "order_info_parent": True,
                        "order_item": True,
                        "pay_record_info": True,
                    },
                    "upms": {"sys_user": False, "sys_user_role": True, "sys_role": True},
                },
                "indexes": idx_map,
                "samples": sample_data,
            }
        ),
    )
    monkeypatch.setattr(
        wizard_runtime.data_preflight,
        "_default_redis_probe",
        lambda cfg: {"reachable": cfg["password"] == redis_secret},
    )
    monkeypatch.setattr(
        wizard_runtime.data_preflight,
        "_default_tsdata_probe",
        lambda url: {"url": url, "reachable": True, "status": 401},
    )
    monkeypatch.setattr(
        wizard_runtime.data_preflight,
        "_default_upms_tenants_probe",
        lambda _url: {
            "reachable": True,
            "status": 200,
            "tenants": [{"id": "1990990768688599040", "name": "捷佳"}],
        },
    )

    report = wizard_runtime.run_live_discovery(
        reg_path,
        environment="custnew",
        api_domain="api.example.cn",
        internal_gateway_addr="10.0.0.31:30899",
        mysql_host="10.0.0.32",
        mysql_password=mysql_secret,
    )
    assert tunnel_closed["closed"] is True
    assert report["ready"] is False
    assert report["db_remediation_plan"]["sql_statements"] == [grant_sql]
    serialized = json.dumps(report, ensure_ascii=False)
    assert mysql_secret not in serialized
    assert redis_secret not in serialized

    tpl = reg_path.parent / "wizard-templates" / "custnew.env"
    assert tpl.is_file()
    assert (tpl.stat().st_mode & 0o777) == 0o600
    assert f'AIOPS_MYSQL_PASSWORD="{mysql_secret}"' in tpl.read_text(encoding="utf-8")
    assert f'AIOPS_REDIS_PASSWORD="{redis_secret}"' in tpl.read_text(encoding="utf-8")

    exec_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        wizard_runtime,
        "_execute_allowlisted_mysql_over_tunnel",
        lambda host, port, user, pwd, stmts: (_assert_eq(pwd, admin_secret), exec_calls.append(stmts)),
    )
    monkeypatch.setattr(
        wizard_runtime.data_preflight,
        "_default_mysql_probe",
        lambda _cfg, _dbs, discover_samples=False: {
            "connected": True,
            "client_host": "%",
            "table_grants": {
                "charging": {
                    "ch_order_info": True,
                    "iot_charging_device": True,
                    "iot_device_offline_monitor": True,
                    "iot_device_failure_records": True,
                    "ch_site": True,
                },
                "mall": {
                    "order_info": True,
                    "order_info_parent": True,
                    "order_item": True,
                    "pay_record_info": True,
                },
                "upms": {"sys_user": True, "sys_user_role": True, "sys_role": True},
            },
            "indexes": idx_map,
            "samples": {"orders": [], "offline_devices": [], "consumer_orders": []},
        },
    )
    fix_res = wizard_runtime.run_live_db_remediation(
        reg_path,
        environment="custnew",
        actor="41:t-admin:u-admin",
        remediation_ids=report["db_remediation_plan"]["remediation_ids"],
        sql_statements=report["db_remediation_plan"]["sql_statements"],
        confirmed_by_operator=True,
        confirmation_digest=report["db_remediation_plan"]["confirmation_digest"],
        admin_mysql_user="root",
        admin_mysql_password=admin_secret,
    )
    assert fix_res["verified_after_fix"] is True
    assert exec_calls == [(grant_sql,)]
    assert admin_secret not in json.dumps(fix_res)


def test_kb_service_live_upload_and_self_validation_via_fastapi(tmp_path: Path, monkeypatch) -> None:
    reg_path = _setup_registry(tmp_path)
    _enroll_test_host(reg_path)

    kb_calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        wizard_runtime,
        "_http_json",
        lambda method, url, headers, body=None, timeout=20.0: (
            kb_calls.append((method, url, headers))
            or (
                (200, {"data": {"biz_tenant_id": "t"}})
                if method == "GET" and url.endswith("/kb/tenants/current")
                else (200, [])
                if method == "GET" and url.endswith("/kb/knowledge-bases")
                else (201, {"data": {"id": "abcdef1234567890abcdef1234567890"}})
                if method == "POST" and url.endswith("/kb/knowledge-bases")
                else (200, {})
                if method == "POST" and url.endswith("/documents/parse")
                else (200, {"data": {"chunk_count": 7, "run": "DONE"}})
            )
        ),
    )
    monkeypatch.setattr(
        wizard_runtime,
        "_http_multipart_upload",
        lambda url, headers, field_name, filename, content_bytes, timeout=45.0: (
            _assert_eq(field_name, "files") or (201, {"data": [{"id": "doc_real_1"}]})
        ),
    )

    revoked_tokens: list[str] = []

    def fake_http(method: str, url: str, headers: dict[str, str], body):
        if method == "POST" and url.endswith("/mallapi/userinfo/phone/login"):
            return 200, json.dumps({"code": 0, "data": {"thirdSession": "ts-live-session-01"}})
        if method == "DELETE" and url.endswith("/auth/token/logout"):
            revoked_tokens.append(headers.get("Authorization", ""))
            return 200, "{}"
        if method == "POST" and url.endswith("/v1/assistant/questions"):
            return 202, json.dumps({"qa_id": f"qa_{len(headers)}", "status": "queued"})
        if method == "GET" and "/v1/assistant/questions/" in url:
            return 200, json.dumps({"qa_id": url.rsplit("/", 1)[1], "status": "completed"})
        return 404, "{}"

    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment_registry_file=str(reg_path),
            environment="41",
            execution_environment="41",
            kb_service_base_url="http://127.0.0.1:29380",
            ragflow_tenant_domain="aiops.local",
        ),
        wizard_hooks=wizard_dispatch.WizardRuntimeHooks(http_caller=fake_http),
    )
    monkeypatch.setattr(
        auth,
        "identity",
        lambda _ctx, _s, **_kw: SimpleNamespace(tenant_id="t-admin", user_id="u-admin"),
    )

    app = FastAPI()

    @app.exception_handler(auth.SetupRefused)
    def _refused(_r, exc):
        return JSONResponse(status_code=400, content={"error": {"code": str(exc)}})

    @app.exception_handler(SetupConflict)
    def _conflict(_r, exc):
        return JSONResponse(status_code=409, content={"error": {"code": str(exc)}})

    management.register(app, context, lambda: {"user": "u-admin"})
    client = TestClient(app)

    r_kb = client.post(
        "/v1/dify/management/environment-wizard",
        json={
            "wizard_action": "kb_install_starter_faq",
            "wizard_payload": {
                "environment": "custnew",
                "kb_id": "kb-consumer-custnew",
                "tenant_id": "1990990768688599040",
            },
        },
    )
    assert r_kb.status_code == 200
    assert r_kb.json()["kb_id"] == "abcdef1234567890abcdef1234567890"
    assert r_kb.json()["document_id"] == "doc_real_1"
    assert r_kb.json()["chunk_count"] == 7
    assert any(c[0] == "POST" and c[1].endswith("/documents/parse") for c in kb_calls)

    r_val = client.post(
        "/v1/dify/management/environment-wizard",
        json={
            "wizard_action": "self_validate",
            "wizard_payload": {
                "environment": "custnew",
                "tenant_id": "1990990768688599040",
                "company_gateway_url": "https://api.example.cn",
                "order_no": "CD202611260001",
                "device_id": "DEV001",
                "offline_start": "2026-11-20T00:00:00+08:00",
                "offline_end": "2026-11-26T23:59:59+08:00",
                "consumer_order_no": "CD202611260002",
                "consumer_reference_id": "starter-faq",
                "consumer_phone": "13900001111",
                "consumer_code": "8888",
                "operator_token": "ephemeral-op-jwt-777",
            },
        },
    )
    assert r_val.status_code == 200
    body = r_val.json()
    assert body["passed"] is True
    assert body["operator_session_revoked"] is True
    assert revoked_tokens == ["Bearer ephemeral-op-jwt-777"]
    assert "ephemeral-op-jwt-777" not in r_val.text
    assert "13900001111" not in r_val.text
