"""V2.1 自助开通向导真实生产运行器（ADR-0358）。

将此前在 ``wizard_dispatch.py`` 中预留的钩子全部接实到生产真实能力：
1. ``run_live_discovery``：经已钉扎指纹的纯密钥 SSH 在客户机运行只读探测，并按需开启短生命期
   SSH 本地端口转发隧道直连客户私网 MySQL/Redis/TSData 执行全量 ``data_preflight``，
   随即把服务端运行配置模板写至受控 ``0600`` ``wizard-templates/<env>.env``（绝不回显明文）；
2. ``run_live_db_remediation``：在已确认二次摘要 + 静态白名单 SQL 下，经瞬时内存管理凭据与
   短生命期 SSH 隧道执行受限 SQL，并立刻以 ``aiops_ro`` 复验；
3. ``upload_to_kb_service``：直连 ``kb-service`` 为客户隔离租户空间自动创建/复用对客与管家知识库，
   执行 ``multipart/form-data`` 文档上传、触发解析并轮询切片完成；
4. ``run_live_self_validation``：通过正式公网 ``https://<api_domain>/v1/assistant/questions``
   以真实标准问句跑通三能力自验。
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.knowledge.kb_onboarding import (
    KbAdminClient,
    OnboardError,
    derive_email,
    generate_password,
)
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant
from aiops_diagnostics.onboarding import (
    data_preflight,
    db_remediation,
    discovery,
    host_enrollment,
    public_smoke,
    registry,
)

#: 从客户机本机 Docker / Redis 配置提取非外泄的连接线索（严格在客户机本机解析，不打印到聊天/前端）。
_REMOTE_DATA_CLUES_SCRIPT = """\
import contextlib
import json, re, subprocess
from pathlib import Path

out = {"redis_password": "", "redis_host": "127.0.0.1", "redis_port": 6379}
for conf in (Path("/www/server/redis/redis.conf"), Path("/etc/redis/redis.conf"), Path("/etc/redis.conf")):
    if conf.is_file():
        try:
            for line in conf.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("requirepass "):
                    out["redis_password"] = stripped.split(None, 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    if out["redis_password"]:
        break

if not out["redis_password"]:
    proc = subprocess.run(
        ["docker", "inspect", "cloud-redis", "--format", "{{json .Config.Cmd}} {{json .Config.Env}}"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    if proc.returncode == 0:
        m = re.search(r"--requirepass\\s+([^\\s,\\]\\"]+)", proc.stdout)
        if m:
            out["redis_password"] = m.group(1)

print("AIOPS_DATA_CLUES_JSON:" + json.dumps(out))
"""

_REMOTE_DATA_CLUES_MARKER = "AIOPS_DATA_CLUES_JSON:"
_KB_ROLE_NAME_PREFIX = {"consumer": "对客知识库", "operator": "管家运维知识库"}
_KB_ROLE_DESC = {
    "consumer": "客户充电小程序/H5 对客自助知识库（与管家运维知识库隔离）",
    "operator": "客户场站运维管家内部知识库（与对客知识库隔离）",
}


class WizardRuntimeError(ValueError):
    """V2.1 真实运行器前置校验或外部调用失败时的稳定错误码异常。"""


def _template_path(registry_path: str | Path, environment: str) -> Path:
    return Path(registry_path).parent / "wizard-templates" / f"{environment}.env"


def _load_existing_template_env(registry_path: str | Path, environment: str) -> dict[str, str]:
    tpl = _template_path(registry_path, environment)
    if tpl.is_file():
        validate_private_file(tpl)
        return {k: str(v or "") for k, v in dotenv_values(tpl, interpolate=False).items()}
    return {}


def _extract_marked_json(raw_output: str, marker: str) -> dict[str, Any]:
    for line in reversed(raw_output.splitlines()):
        cleaned = line.strip()
        idx = cleaned.find(marker)
        if idx >= 0:
            parsed = json.loads(cleaned[idx + len(marker) :].strip())
            if isinstance(parsed, dict):
                return parsed
    raise WizardRuntimeError("ENVIRONMENT_DISCOVERY_PROBE_MALFORMED")


def _probe_remote_host_facts(
    conn_info: dict[str, Any],
    *,
    environment: str,
    api_domain: str,
    internal_gateway_addr: str,
) -> dict[str, Any]:
    script = discovery.build_remote_discovery_probe_script(
        environment=environment,
        api_domain=api_domain,
        internal_gateway_addr=internal_gateway_addr,
    )
    proc = host_enrollment.run_pinned_ssh(conn_info, ["python3", "-"], stdin_text=script, timeout=35.0)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise WizardRuntimeError("ENVIRONMENT_DISCOVERY_REMOTE_PROBE_FAILED")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    parsed = json.loads(lines[-1])
    if not isinstance(parsed, dict):
        raise WizardRuntimeError("ENVIRONMENT_DISCOVERY_REMOTE_PROBE_FAILED")
    return parsed


def _probe_remote_redis_clues(conn_info: dict[str, Any]) -> dict[str, Any]:
    proc = host_enrollment.run_pinned_ssh(
        conn_info, ["python3", "-"], stdin_text=_REMOTE_DATA_CLUES_SCRIPT, timeout=20.0
    )
    if proc.returncode != 0 or _REMOTE_DATA_CLUES_MARKER not in proc.stdout:
        return {"redis_host": "127.0.0.1", "redis_port": 6379, "redis_password": ""}
    return _extract_marked_json(proc.stdout, _REMOTE_DATA_CLUES_MARKER)


def run_live_discovery(
    registry_path: str | Path,
    *,
    environment: str,
    api_domain: str,
    internal_gateway_addr: str = "",
    mysql_host: str = "",
    mysql_port: int = 3306,
    mysql_user: str = "aiops_ro",
    mysql_password: str = "",
    redis_host: str = "",
    redis_port: int = 6379,
    redis_password: str = "",
) -> dict[str, Any]:
    """在已钉扎客户机执行只读环境探测 + 经短生命期 SSH 隧道直检数据面并落盘 0600 模板。"""
    try:
        conn_info = host_enrollment.load_enrolled_host_connection(registry_path, environment)
        host_facts = _probe_remote_host_facts(
            conn_info,
            environment=environment,
            api_domain=api_domain,
            internal_gateway_addr=internal_gateway_addr,
        )
        existing_tpl = _load_existing_template_env(registry_path, environment)

        eff_mysql_host = mysql_host.strip() or existing_tpl.get("AIOPS_MYSQL_HOST", "")
        eff_mysql_port = int(mysql_port or existing_tpl.get("AIOPS_MYSQL_PORT") or 3306)
        eff_mysql_user = mysql_user.strip() or existing_tpl.get("AIOPS_MYSQL_USER") or "aiops_ro"
        eff_mysql_password = mysql_password or existing_tpl.get("AIOPS_MYSQL_PASSWORD", "")

        eff_redis_host = redis_host.strip() or existing_tpl.get("AIOPS_REDIS_HOST", "127.0.0.1")
        eff_redis_port = int(redis_port or existing_tpl.get("AIOPS_REDIS_PORT") or 6379)
        eff_redis_password = redis_password or existing_tpl.get("AIOPS_REDIS_PASSWORD", "")
        if not eff_redis_password:
            clues = _probe_remote_redis_clues(conn_info)
            if clues.get("redis_password"):
                eff_redis_password = str(clues["redis_password"])
            if not redis_host.strip() and clues.get("redis_host"):
                eff_redis_host = str(clues["redis_host"])

        ts_candidates = list(host_facts.get("tsdata_loopback_candidates") or [])
        configured_ts = existing_tpl.get("AIOPS_GATEWAY_OPERATOR_TSDATA_URL", "")
        if not configured_ts and ts_candidates:
            configured_ts = str(ts_candidates[0])

        tunnel_targets: dict[str, tuple[str, int]] = {"redis": (eff_redis_host, eff_redis_port)}
        if eff_mysql_host:
            tunnel_targets["mysql"] = (eff_mysql_host, eff_mysql_port)
        ts_map: dict[str, str] = {}
        if configured_ts:
            parsed_ts = urlsplit(configured_ts)
            if parsed_ts.hostname:
                ts_port = parsed_ts.port or (443 if parsed_ts.scheme == "https" else 80)
                tunnel_targets["tsdata"] = (parsed_ts.hostname, ts_port)
                ts_map["primary_scheme"] = parsed_ts.scheme or "http"
                ts_map["primary_url"] = configured_ts

        company_gw_url = f"https://{api_domain.strip().lower()}"
        with host_enrollment.open_pinned_ssh_tunnel(conn_info, tunnel_targets) as local_ports:
            tunnel_ts_url = (
                f"{ts_map['primary_scheme']}://127.0.0.1:{local_ports['tsdata']}"
                if "tsdata" in local_ports
                else ""
            )
            env_values: dict[str, str | None] = {
                "AIOPS_MYSQL_HOST": "127.0.0.1" if "mysql" in local_ports else "",
                "AIOPS_MYSQL_PORT": str(local_ports.get("mysql", eff_mysql_port)),
                "AIOPS_MYSQL_USER": eff_mysql_user,
                "AIOPS_MYSQL_PASSWORD": eff_mysql_password,
                "AIOPS_REDIS_HOST": "127.0.0.1",
                "AIOPS_REDIS_PORT": str(local_ports["redis"]),
                "AIOPS_REDIS_PASSWORD": eff_redis_password,
                "AIOPS_GATEWAY_OPERATOR_TSDATA_URL": tunnel_ts_url,
                "AIOPS_UPMS_BASE_URL": company_gw_url,
            }
            preflight_report = data_preflight.run_data_preflight(
                env_values,
                company_gateway_url=company_gw_url,
                tsdata_fallback_urls=(),
                discover_samples=True,
            )

        # 将隧道视角还原为目标客户机视角的正式内网地址后再写入 0600 服务端模板
        discovered = preflight_report.get("discovered") or {}
        if discovered.get("healthy_tsdata_url") and configured_ts:
            discovered["healthy_tsdata_url"] = configured_ts
        for check in preflight_report.get("checks") or []:
            if isinstance(check, dict) and check.get("category") == "tsdata":
                detail = check.get("detail")
                if isinstance(detail, dict) and configured_ts and "url" in detail:
                    detail["url"] = configured_ts

        if eff_mysql_host and eff_mysql_password:
            extra = {
                "AIOPS_UPMS_BASE_URL": company_gw_url,
                "AIOPS_GATEWAY_PUBLIC_BASE_URL": company_gw_url,
                "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": company_gw_url,
                "AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX": "app:3rd_session:",
                "AIOPS_MYSQL_DATABASE": existing_tpl.get("AIOPS_MYSQL_DATABASE") or "cloud_charging_pile",
                "AIOPS_MYSQL_TIMEZONE": str(host_facts.get("timezone") or "Asia/Shanghai"),
            }
            if internal_gateway_addr.strip():
                extra["AIOPS_INTERNAL_GATEWAY_ADDR"] = internal_gateway_addr.strip()
            discovery.write_server_side_config_template(
                _template_path(registry_path, environment),
                mysql_host=eff_mysql_host,
                mysql_port=eff_mysql_port,
                mysql_user=eff_mysql_user,
                mysql_password=eff_mysql_password,
                redis_host=eff_redis_host,
                redis_port=eff_redis_port,
                redis_password=eff_redis_password,
                tsdata_url=configured_ts or "http://127.0.0.1:30888",
                extra_entries=extra,
            )

        return discovery.parse_and_enrich_discovery_report(
            environment=environment,
            host_facts=host_facts,
            data_preflight_report=preflight_report,
        )
    finally:
        mysql_password = ""
        redis_password = ""


def _execute_allowlisted_mysql_over_tunnel(
    host: str,
    port: int,
    user: str,
    password: str,
    statements: tuple[str, ...],
) -> None:
    import pymysql

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def run_live_db_remediation(
    registry_path: str | Path,
    *,
    environment: str,
    actor: str,
    remediation_ids: list[str],
    sql_statements: list[str],
    confirmed_by_operator: bool,
    confirmation_digest: str,
    admin_mysql_host: str = "",
    admin_mysql_port: int = 0,
    admin_mysql_user: str = "",
    admin_mysql_password: str = "",
    mysql_host: str = "",
    mysql_port: int = 3306,
    mysql_user: str = "aiops_ro",
    mysql_password: str = "",
) -> dict[str, Any]:
    """经二次确认 + 静态白名单在客户机隧道内执行受限 SQL 并立刻以 aiops_ro 复验。"""
    try:
        conn_info = host_enrollment.load_enrolled_host_connection(registry_path, environment)
        tpl = _load_existing_template_env(registry_path, environment)
        eff_ro_host = mysql_host.strip() or tpl.get("AIOPS_MYSQL_HOST", "")
        eff_ro_port = int(mysql_port or tpl.get("AIOPS_MYSQL_PORT") or 3306)
        eff_ro_user = mysql_user.strip() or tpl.get("AIOPS_MYSQL_USER") or "aiops_ro"
        eff_ro_password = mysql_password or tpl.get("AIOPS_MYSQL_PASSWORD", "")

        eff_admin_host = admin_mysql_host.strip() or eff_ro_host
        eff_admin_port = int(admin_mysql_port or eff_ro_port or 3306)
        eff_admin_user = admin_mysql_user.strip()
        eff_admin_password = admin_mysql_password
        if not eff_admin_host or not eff_admin_user or not eff_admin_password:
            raise db_remediation.RemediationRefused("ENVIRONMENT_DB_REMEDIATION_ADMIN_CREDENTIAL_REQUIRED")

        audit_path = Path(registry_path).parent / "db-remediation-audit.jsonl"
        eff_redis_host = tpl.get("AIOPS_REDIS_HOST", "").strip()
        eff_redis_port = int(tpl.get("AIOPS_REDIS_PORT") or 6379)
        eff_redis_password = tpl.get("AIOPS_REDIS_PASSWORD", "")
        configured_ts = tpl.get("AIOPS_GATEWAY_OPERATOR_TSDATA_URL", "").strip()
        upms_url = tpl.get("AIOPS_UPMS_BASE_URL", "").strip()

        targets: dict[str, tuple[str, int]] = {"admin_mysql": (eff_admin_host, eff_admin_port)}
        if eff_ro_host and eff_ro_password:
            targets["ro_mysql"] = (eff_ro_host, eff_ro_port)
        if eff_redis_host:
            targets["redis"] = (eff_redis_host, eff_redis_port)
        ts_scheme = "http"
        if configured_ts:
            parsed_ts = urlsplit(configured_ts)
            if parsed_ts.hostname:
                ts_scheme = parsed_ts.scheme or "http"
                targets["tsdata"] = (
                    parsed_ts.hostname,
                    parsed_ts.port or (443 if ts_scheme == "https" else 80),
                )

        with host_enrollment.open_pinned_ssh_tunnel(conn_info, targets) as local_ports:

            def _exec(stmts: tuple[str, ...]) -> None:
                _execute_allowlisted_mysql_over_tunnel(
                    "127.0.0.1",
                    local_ports["admin_mysql"],
                    eff_admin_user,
                    eff_admin_password,
                    stmts,
                )

            def _reverify() -> dict[str, Any]:
                if "ro_mysql" not in local_ports:
                    return {"ready": False, "reason": "RO_CREDENTIALS_MISSING"}
                ts_url = f"{ts_scheme}://127.0.0.1:{local_ports['tsdata']}" if "tsdata" in local_ports else ""
                return data_preflight.run_data_preflight(
                    {
                        "AIOPS_MYSQL_HOST": "127.0.0.1",
                        "AIOPS_MYSQL_PORT": str(local_ports["ro_mysql"]),
                        "AIOPS_MYSQL_USER": eff_ro_user,
                        "AIOPS_MYSQL_PASSWORD": eff_ro_password,
                        "AIOPS_REDIS_HOST": "127.0.0.1" if "redis" in local_ports else "",
                        "AIOPS_REDIS_PORT": str(local_ports.get("redis", eff_redis_port)),
                        "AIOPS_REDIS_PASSWORD": eff_redis_password,
                        "AIOPS_GATEWAY_OPERATOR_TSDATA_URL": ts_url,
                        "AIOPS_UPMS_BASE_URL": upms_url,
                    },
                    tsdata_fallback_urls=(),
                    discover_samples=False,
                )

            return db_remediation.execute_confirmed_db_remediation(
                environment=environment,
                actor=actor,
                remediation_ids=remediation_ids,
                sql_statements=sql_statements,
                confirmed_by_operator=confirmed_by_operator,
                confirmation_digest=confirmation_digest,
                executor=_exec,
                reverify_runner=_reverify,
                audit_log_path=audit_path,
            )
    finally:
        admin_mysql_password = ""
        mysql_password = ""


def _http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> tuple[int, dict[str, Any]]:
    raw_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req_headers = dict(headers)
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = Request(url, data=raw_bytes, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(text) if text.strip() else {}
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(text) if text.strip() else {}
        except ValueError:
            return int(exc.code), {"raw": text[:400]}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise WizardRuntimeError("ENVIRONMENT_KB_SERVICE_UNAVAILABLE") from exc


def _http_multipart_upload(
    url: str,
    headers: dict[str, str],
    *,
    field_name: str,
    filename: str,
    content_bytes: bytes,
    timeout: float = 45.0,
) -> tuple[int, dict[str, Any]]:
    boundary = f"----aiops-kb-{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_name = filename.replace('"', "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    payload = head + content_bytes + tail
    req = Request(
        url,
        data=payload,
        headers={
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(text) if text.strip() else {}
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(text) if text.strip() else {}
        except ValueError:
            return int(exc.code), {"raw": text[:400]}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise WizardRuntimeError("ENVIRONMENT_KB_UPLOAD_FAILED") from exc


def _extract_kbs_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]
    return []


def _extract_uploaded_doc_id(payload: Any) -> str:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return str(payload[0].get("id") or payload[0].get("doc_id") or "")
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("id") or data[0].get("doc_id") or "")
        if isinstance(data, dict):
            docs = data.get("documents") or data.get("items")
            if isinstance(docs, list) and docs and isinstance(docs[0], dict):
                return str(docs[0].get("id") or docs[0].get("doc_id") or "")
            return str(data.get("id") or data.get("doc_id") or "")
        return str(payload.get("id") or payload.get("doc_id") or "")
    return ""


def _resolve_default_tenant_id(registry_path: str | Path, environment: str, tenant_id: str) -> str:
    tid = tenant_id.strip()
    if tid:
        return tid
    body = registry.load(registry_path)
    for row in body.get("environments", []):
        if row.get("id") == environment and row.get("tenants"):
            return str(row["tenants"][0]["id"])
    tpl_file = _template_path(registry_path, environment)
    if tpl_file.is_file():
        vals = _load_existing_template_env(registry_path, environment)
        upms_url = vals.get("AIOPS_UPMS_BASE_URL", "")
        if upms_url:
            probe = data_preflight._default_upms_tenants_probe(upms_url)
            if probe.get("tenants"):
                return str(probe["tenants"][0]["id"])
    raise WizardRuntimeError("ENVIRONMENT_KB_TENANT_REQUIRED")


def upload_to_kb_service(
    settings: Any,
    registry_path: str | Path,
    *,
    environment: str,
    tenant_id: str,
    target_role: str,
    kb_id: str,
    filename: str,
    content_bytes: bytes,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """直连 kb-service 为隔离环境/租户自动创建或定位目标角色知识库、上传并触发解析切片。"""
    base_url = str(getattr(settings, "kb_service_base_url", "") or "").strip().rstrip("/")
    if not base_url:
        raise WizardRuntimeError("ENVIRONMENT_KB_SERVICE_UNCONFIGURED")
    resolved_tenant = _resolve_default_tenant_id(registry_path, environment, tenant_id)
    scoped_tenant = knowledge_tenant(resolved_tenant, environment)
    domain = str(getattr(settings, "ragflow_tenant_domain", "") or "aiops.local").strip() or "aiops.local"
    admin = KbAdminClient(base_url, environment=environment)
    headers = {"tenant-id": scoped_tenant}
    cur_code, _ = _http_json("GET", f"{base_url}/kb/tenants/current", headers)
    if cur_code != 200:
        with contextlib.suppress(OnboardError):
            admin.register_tenant(
                tenant_id=resolved_tenant,
                email=derive_email(scoped_tenant, domain=domain),
                password=generate_password(),
            )
        with_repair_ok = False
        try:
            admin.repair_models(resolved_tenant)
            with_repair_ok = True
        except OnboardError:
            pass
        if not with_repair_ok:
            check_code, _ = _http_json("GET", f"{base_url}/kb/tenants/current", headers)
            if check_code != 200:
                raise WizardRuntimeError("ENVIRONMENT_KB_TENANT_UNAVAILABLE")

    # 如果前端传的是占位前缀 kb-consumer-* / kb-operator-*，自动发现或按角色创建隔离知识库
    effective_kb_id = kb_id.strip()
    is_placeholder = effective_kb_id.startswith(("kb-consumer-", "kb-operator-")) or not re.fullmatch(
        r"[0-9a-f]{24,64}", effective_kb_id
    )
    if is_placeholder:
        list_code, list_body = _http_json("GET", f"{base_url}/kb/knowledge-bases", headers)
        if list_code != 200:
            raise WizardRuntimeError("ENVIRONMENT_KB_LIST_FAILED")
        kbs = _extract_kbs_list(list_body)
        prefix = _KB_ROLE_NAME_PREFIX[target_role]
        matched = [
            item
            for item in kbs
            if str(item.get("name") or "").startswith(prefix)
            or (target_role == "consumer" and "对客" in str(item.get("name") or ""))
            or (target_role == "operator" and "管家" in str(item.get("name") or ""))
        ]
        if matched:
            effective_kb_id = str(matched[0].get("id") or matched[0].get("knowledge_base_id") or "")
        else:
            create_name = f"{prefix}-{environment}"
            c_code, c_body = _http_json(
                "POST",
                f"{base_url}/kb/knowledge-bases",
                headers,
                {"name": create_name, "description": _KB_ROLE_DESC[target_role]},
            )
            if c_code not in (200, 201):
                raise WizardRuntimeError("ENVIRONMENT_KB_CREATE_FAILED")
            data_obj = (
                c_body.get("data")
                if isinstance(c_body, dict) and isinstance(c_body.get("data"), dict)
                else c_body
            )
            effective_kb_id = str(
                (data_obj or {}).get("id") or (data_obj or {}).get("knowledge_base_id") or ""
            )
        if not effective_kb_id:
            raise WizardRuntimeError("ENVIRONMENT_KB_ID_UNRESOLVED")

    up_code, up_body = _http_multipart_upload(
        f"{base_url}/kb/knowledge-bases/{effective_kb_id}/documents",
        headers,
        field_name="files",
        filename=filename,
        content_bytes=content_bytes,
    )
    if up_code not in (200, 201):
        raise WizardRuntimeError("ENVIRONMENT_KB_UPLOAD_FAILED")
    doc_id = _extract_uploaded_doc_id(up_body)
    if not doc_id:
        raise WizardRuntimeError("ENVIRONMENT_KB_DOC_ID_MISSING")

    parse_code, _ = _http_json(
        "POST",
        f"{base_url}/kb/knowledge-bases/{effective_kb_id}/documents/parse",
        headers,
        {"doc_ids": [doc_id]},
    )
    if parse_code not in (200, 201, 202):
        raise WizardRuntimeError("ENVIRONMENT_KB_PARSE_FAILED")

    chunk_count = 0
    for _ in range(12):
        d_code, d_body = _http_json(
            "GET",
            f"{base_url}/kb/knowledge-bases/{effective_kb_id}/documents/{doc_id}",
            headers,
        )
        if d_code == 200 and isinstance(d_body, dict):
            doc_obj = d_body.get("data") if isinstance(d_body.get("data"), dict) else d_body
            chunk_count = int((doc_obj or {}).get("chunk_count") or 0)
            run_state = str((doc_obj or {}).get("run") or (doc_obj or {}).get("status") or "").upper()
            if chunk_count > 0 or run_state in {"DONE", "3"}:
                break
            if run_state in {"FAIL", "4"}:
                raise WizardRuntimeError("ENVIRONMENT_KB_PARSE_FAILED")
        sleep_fn(2.0)

    return {
        "kb_id": effective_kb_id,
        "document_id": doc_id,
        "chunk_count": chunk_count,
    }


def run_live_self_validation(
    cases_payload: dict[str, Any],
    *,
    http_caller: public_smoke.HttpCaller | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """向正式公网 HTTPS 入口并发/顺序运行三能力标准问句并聚合通过结论。"""
    base = str(cases_payload.get("company_gateway_url") or "").strip().rstrip("/")
    tid = str(cases_payload.get("tenant_id") or "").strip()
    consumer_session = str(cases_payload.get("consumer_session") or "").strip()
    operator_token = str(cases_payload.get("operator_token") or "").strip()
    if operator_token.lower().startswith("bearer "):
        operator_token = operator_token[7:].strip()
    cases = cases_payload.get("cases") or {}
    caller = http_caller or public_smoke._default_http_caller

    op_headers = {
        "Authorization": f"Bearer {operator_token}",
        "X-Business-Entry": "operator",
        "tenant-id": tid,
    }
    probes = {
        "consumer": (
            {"third-session": consumer_session, "tenant-id": tid},
            str((cases.get("consumer") or {}).get("question") or ""),
        ),
        "order": (
            op_headers,
            str((cases.get("order") or {}).get("question") or ""),
        ),
        "offline": (
            op_headers,
            str((cases.get("offline") or {}).get("question") or ""),
        ),
    }

    def _run_probe(hdrs: dict[str, str], question: str) -> dict[str, Any]:
        return public_smoke._poll_formal_qa(
            caller,
            base,
            hdrs,
            question,
            timeout_seconds=90.0,
            poll_interval_seconds=2.0,
            sleep_fn=sleep_fn,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {key: pool.submit(_run_probe, hdrs, q) for key, (hdrs, q) in probes.items()}
        results = {key: futures[key].result() for key in ("consumer", "order", "offline")}

    return {
        "capabilities": {
            key: {
                "state": "passed" if results[key].get("passed") else "failed",
                "run_id": str(results[key].get("qa_id") or ""),
                "reason": str(results[key].get("reason") or ""),
            }
            for key in ("consumer", "order", "offline")
        }
    }
