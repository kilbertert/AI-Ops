"""客户主机只读自动环境发现与脱敏体检单构造（ADR-0358 P1-2）。

通过已钉扎指纹的纯密钥通道在客户机执行只读发现探针，自动识别：
1. 活跃租户清单（公司 UPMS ``/upms/tenant/list``）；
2. Web 反向代理类型（宝塔 / 标准 Nginx）与 ``api.<domain>`` 扩展配置目录；
3. Python 小版本、主机时区与本机回环时序服务（如 ``cloud-tsdata-biz:30888``）；
4. MySQL 只读权限缺口、必选索引缺口与脱敏候选验收样本；
5. 对所有自动生成的受限修复 SQL 计算确定性二次确认摘要（``confirmation_digest``）。

严禁将任何数据库/Redis 明文密码、私钥或原始手机号返回前端浏览器。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiops_diagnostics.common.private_files import ensure_private_directory, write_private_text

DISCOVERY_SCHEMA = "aiops-environment-discovery/1"

#: 严禁出现在前端可见探测结果中的敏感键名片段。
_FORBIDDEN_SECRET_TOKENS = ("password", "passwd", "secret", "private_key", "token", "third_session")


class DiscoveryRefused(ValueError):
    """环境探测输入或回传结果不合法时的稳定拒绝异常。"""


def _is_safe_env_id(value: str) -> bool:
    if not isinstance(value, str) or not (1 <= len(value) <= 32):
        return False
    return all(ch.isascii() and (ch.islower() or ch.isdigit() or ch in "_-") for ch in value) and (
        value[0].isalnum() and value[-1].isalnum()
    )


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_URL_INVALID")
    raw = url.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_URL_INVALID")
    if parsed.query or parsed.fragment:
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_URL_INVALID")
    return raw


def _contains_secret_key(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, val in obj.items():
            lowered = str(key).lower()
            if any(bad in lowered for bad in _FORBIDDEN_SECRET_TOKENS):
                return True
            if _contains_secret_key(val):
                return True
    elif isinstance(obj, list):
        return any(_contains_secret_key(item) for item in obj)
    return False


def compute_remediation_confirmation_digest(
    environment: str,
    remediation_ids: list[str] | tuple[str, ...],
    sql_statements: list[str] | tuple[str, ...],
) -> str:
    """为一组待二次确认的受限修复 SQL 计算确定性 SHA-256 摘要，防篡改与防跨环境误执行。"""
    if not _is_safe_env_id(environment):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_ENV_INVALID")
    canonical = json.dumps(
        {
            "environment": environment,
            "remediation_ids": sorted(str(x) for x in remediation_ids),
            "sql_statements": [str(s).strip() for s in sql_statements],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_remote_discovery_probe_script(
    *,
    environment: str,
    api_domain: str,
    internal_gateway_addr: str = "",
) -> str:
    """构造在客户主机以只读方式运行的环境发现 Python 脚本（绝不打印任何凭据）。"""
    if not _is_safe_env_id(environment):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_ENV_INVALID")
    domain = api_domain.strip().lower()
    if not domain or any(ch.isspace() or ch in "/:@" for ch in domain):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_DOMAIN_INVALID")
    gw_url = _validate_https_url(f"https://{domain}")
    return f"""\
import json, os, socket, sys, urllib.request
from pathlib import Path

env_id = {environment!r}
api_domain = {domain!r}
gw_url = {gw_url!r}
internal_gw = {internal_gateway_addr.strip()!r}

py_minor = f"{{sys.version_info.major}}.{{sys.version_info.minor}}"
tz = "Asia/Shanghai"
tz_file = Path("/etc/timezone")
if tz_file.is_file():
    val = tz_file.read_text(encoding="utf-8", errors="ignore").strip()
    if val:
        tz = val

nginx_flavor = "none"
nginx_ext_dir = ""
baota_ext = Path("/www/server/panel/vhost/nginx/extension") / api_domain
if Path("/www/server/panel/vhost/nginx").is_dir():
    nginx_flavor = "baota"
    nginx_ext_dir = str(baota_ext)
elif Path("/etc/nginx").is_dir():
    nginx_flavor = "standard"
    nginx_ext_dir = "/etc/nginx/conf.d"

def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False

tsdata_candidates = []
if _port_open("127.0.0.1", 30888):
    tsdata_candidates.append("http://127.0.0.1:30888")

tenants = []
try:
    req = urllib.request.Request(gw_url + "/upms/tenant/list", headers={{"Accept": "application/json"}})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace")).get("data") or []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                tenants.append({{
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                    "code": str(item.get("code") or ""),
                    "status": str(item.get("status") or ""),
                }})
except Exception:
    pass

print(json.dumps({{
    "environment": env_id,
    "hostname": socket.gethostname(),
    "python_minor": py_minor,
    "timezone": tz,
    "nginx_flavor": nginx_flavor,
    "nginx_extension_dir": nginx_ext_dir,
    "internal_gateway_addr": internal_gw,
    "tsdata_loopback_candidates": tsdata_candidates,
    "tenants": tenants,
}}, ensure_ascii=False))
"""


def parse_and_enrich_discovery_report(
    *,
    environment: str,
    host_facts: dict[str, Any],
    data_preflight_report: dict[str, Any],
) -> dict[str, Any]:
    """合并主机只读探测与数据面预检结果，剔除秘密并附带受限修复二次确认摘要。"""
    if not _is_safe_env_id(environment):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_ENV_INVALID")
    if not isinstance(host_facts, dict) or not isinstance(data_preflight_report, dict):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_REPORT_INVALID")
    if _contains_secret_key(host_facts) or _contains_secret_key(data_preflight_report):
        raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_SECRET_LEAK_BLOCKED")

    checks = list(data_preflight_report.get("checks") or [])
    remediations = list(data_preflight_report.get("remediations") or [])
    discovered = dict(data_preflight_report.get("discovered") or {})

    # 汇总全部可自动代执行的 MySQL 修复 SQL 并绑定二次确认摘要
    auto_db_rems = [
        r
        for r in remediations
        if isinstance(r, dict)
        and r.get("auto_fixable") is True
        and r.get("kind") in {"mysql_grant", "mysql_index"}
        and r.get("sql_statements")
    ]
    db_rem_ids = [str(r["id"]) for r in auto_db_rems]
    db_sqls = [str(s) for r in auto_db_rems for s in r.get("sql_statements", [])]
    db_confirmation_digest = (
        compute_remediation_confirmation_digest(environment, db_rem_ids, db_sqls) if db_sqls else ""
    )

    tenants = discovered.get("tenants") or host_facts.get("tenants") or []
    ready = bool(data_preflight_report.get("ready")) and not any(
        isinstance(c, dict) and c.get("state") == "blocked" for c in checks
    )
    return {
        "schema": DISCOVERY_SCHEMA,
        "environment": environment,
        "ready": ready,
        "state": "passed" if ready else "blocked",
        "host": {
            "hostname": str(host_facts.get("hostname") or ""),
            "python_minor": str(host_facts.get("python_minor") or ""),
            "timezone": str(host_facts.get("timezone") or "Asia/Shanghai"),
            "nginx_flavor": str(host_facts.get("nginx_flavor") or "none"),
            "nginx_extension_dir": str(host_facts.get("nginx_extension_dir") or ""),
        },
        "checks": checks,
        "remediations": remediations,
        "db_remediation_plan": {
            "has_auto_fixable_db_gaps": bool(db_sqls),
            "remediation_ids": db_rem_ids,
            "sql_statements": db_sqls,
            "confirmation_digest": db_confirmation_digest,
        },
        "discovered": {
            "tenants": tenants,
            "healthy_tsdata_url": discovered.get("healthy_tsdata_url"),
            "sample_candidates": discovered.get("sample_candidates")
            or {"orders": [], "offline_devices": [], "consumer_orders": []},
        },
    }


def write_server_side_config_template(
    output_path: Path,
    *,
    mysql_host: str,
    mysql_port: int,
    mysql_user: str,
    mysql_password: str,
    redis_host: str,
    redis_port: int,
    redis_password: str,
    tsdata_url: str,
    extra_entries: dict[str, str] | None = None,
) -> dict[str, Any]:
    """仅在控制面受控 0600 文件落盘配置模板，返回 SHA-256 摘要，严禁回显正文。"""
    ensure_private_directory(output_path.parent)
    lines = [
        f'AIOPS_MYSQL_HOST="{mysql_host.strip()}"',
        f"AIOPS_MYSQL_PORT={int(mysql_port)}",
        f'AIOPS_MYSQL_USER="{mysql_user.strip()}"',
        f'AIOPS_MYSQL_PASSWORD="{mysql_password}"',
        f'AIOPS_REDIS_HOST="{redis_host.strip()}"',
        f"AIOPS_REDIS_PORT={int(redis_port)}",
        f'AIOPS_REDIS_PASSWORD="{redis_password}"',
        f'AIOPS_GATEWAY_OPERATOR_TSDATA_URL="{tsdata_url.strip()}"',
    ]
    for key, value in sorted((extra_entries or {}).items()):
        if not key.isascii() or not all(c.isupper() or c.isdigit() or c == "_" for c in key):
            raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_CONFIG_KEY_INVALID")
        if "\n" in value or "\r" in value:
            raise DiscoveryRefused("ENVIRONMENT_DISCOVERY_CONFIG_VALUE_INVALID")
        lines.append(f'{key}="{value}"')
    content = "\n".join(lines) + "\n"
    write_private_text(output_path, content)
    return {
        "written": True,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
