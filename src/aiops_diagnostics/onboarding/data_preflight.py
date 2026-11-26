"""目标业务环境数据面全量预检与受限修复规划器（ADR-0358 P0-1）。

集中负责在部署与正式发布前一次性暴露客户数据面缺口，避免直到公网 HTTPS 阶段才触发
``503 PLATFORM_UNAVAILABLE`` 或 ``OPERATOR_OFFLINE_UNAVAILABLE``：
1. MySQL 最小只读权限矩阵（``cloud_charging_pile``、``qumall_mall``、``qumall_upms`` 三张平台表）；
2. MySQL 必选复合索引检查（``iot_device_offline_monitor``、``ch_order_info``）；
3. 时序服务（``tsdata``）主地址与本机直连回环候选自动探测；
4. Redis 连通性与公司 UPMS 活跃租户发现；
5. 脱敏候选验收样本采样（零手机号/令牌/密码回显）。

本模块仅执行只读检查与白名单 SQL 生成，不执行任何 DDL/GRANT 写操作，也不输出任何凭据。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

#: 严格标识符白名单：用于库名、表名、索引名与只读用户名校验，阻断 SQL 注入。
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SAFE_MYSQL_HOST = re.compile(r"^[A-Za-z0-9._:%-]{1,128}$")

#: AI-Ops 运行期必需的最小只读库表集合（键为逻辑库键，值为表名元组）。
REQUIRED_MYSQL_TABLES: dict[str, tuple[str, ...]] = {
    "charging": (
        "ch_order_info",
        "iot_charging_device",
        "iot_device_offline_monitor",
        "iot_device_failure_records",
        "ch_site",
    ),
    "mall": (
        "order_info",
        "order_info_parent",
        "order_item",
        "pay_record_info",
    ),
    "upms": (
        "sys_user",
        "sys_user_role",
        "sys_role",
    ),
}

#: 运行期必选 MySQL 索引定义：(逻辑库键, 表名, 规范索引名, 前缀列元组)。
REQUIRED_MYSQL_INDEXES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "charging",
        "iot_device_offline_monitor",
        "aiops_offline_device_time",
        ("device_id", "offline_time"),
    ),
    (
        "charging",
        "ch_order_info",
        "ch_order_info_device_id_index",
        ("device_id",),
    ),
)

#: 受限白名单允许自动生成的修复动作种类。
ALLOWED_REMEDIATION_KINDS = frozenset({"mysql_grant", "mysql_index", "tsdata_url"})

#: 常见本机回环时序服务候选（应对客户网关 ``/tsdata/**`` 路由指向失效端口的场景）。
DEFAULT_TSDATA_LOOPBACK_CANDIDATES: tuple[str, ...] = ("http://127.0.0.1:30888",)


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    id: str
    category: str
    state: str  # "passed" | "blocked" | "warning"
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    remediation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "state": self.state,
            "summary": self.summary,
            "detail": dict(self.detail),
        }
        if self.remediation_id:
            out["remediation_id"] = self.remediation_id
        return out


@dataclass(frozen=True, slots=True)
class RemediationAction:
    id: str
    kind: str  # "mysql_grant" | "mysql_index" | "tsdata_url"
    title: str
    description: str
    auto_fixable: bool
    risk_level: str  # "low" | "medium"
    sql_statements: tuple[str, ...] = ()
    suggested_config: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "auto_fixable": self.auto_fixable,
            "risk_level": self.risk_level,
        }
        if self.sql_statements:
            out["sql_statements"] = list(self.sql_statements)
        if self.suggested_config:
            out["suggested_config"] = dict(self.suggested_config)
        return out


def _validate_ident(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENT.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _validate_mysql_host(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_MYSQL_HOST.fullmatch(value):
        raise ValueError("invalid mysql client host")
    return value


def build_upms_grant_sql(
    mysql_user: str,
    mysql_client_host: str,
    missing_tables: list[str] | tuple[str, ...],
    *,
    upms_database: str = "qumall_upms",
) -> tuple[str, ...]:
    """仅允许为 ``qumall_upms.{sys_user,sys_user_role,sys_role}`` 生成最小 ``GRANT SELECT``。"""
    user = _validate_ident(mysql_user, "mysql_user")
    host = _validate_mysql_host(mysql_client_host)
    db = _validate_ident(upms_database, "upms_database")
    allowed = set(REQUIRED_MYSQL_TABLES["upms"])
    statements: list[str] = []
    for table in missing_tables:
        if table not in allowed:
            raise ValueError(f"table {table!r} is not in UPMS grant allowlist")
        statements.append(f"GRANT SELECT ON `{db}`.`{table}` TO '{user}'@'{host}';")
    return tuple(statements)


def build_missing_index_sql(
    missing_indexes: list[tuple[str, str, str, tuple[str, ...]]],
    *,
    charging_database: str = "cloud_charging_pile",
) -> tuple[str, ...]:
    """仅允许为白名单内离线/订单表生成在线无锁 DDL（``ALGORITHM=INPLACE, LOCK=NONE``）。"""
    db = _validate_ident(charging_database, "charging_database")
    allowed_map = {(table, index_name): cols for _, table, index_name, cols in REQUIRED_MYSQL_INDEXES}
    statements: list[str] = []
    for _, table, index_name, cols in missing_indexes:
        expected_cols = allowed_map.get((table, index_name))
        if expected_cols is None or tuple(cols) != expected_cols:
            raise ValueError(f"index {table}.{index_name} is not in index allowlist")
        cols_sql = ", ".join(f"`{col}`" for col in expected_cols)
        statements.append(
            f"ALTER TABLE `{db}`.`{table}` ADD INDEX `{index_name}` ({cols_sql}), "
            "ALGORITHM=INPLACE, LOCK=NONE;"
        )
    return tuple(statements)


def has_leading_index(index_rows: list[dict[str, Any]], leading_columns: tuple[str, ...]) -> str | None:
    """检查 ``SHOW INDEX`` 结果中是否存在前缀列匹配的索引，命中返回索引名。"""
    by_name: dict[str, dict[int, str]] = {}
    for row in index_rows:
        key_name = str(row.get("Key_name") or row.get("key_name") or "")
        col_name = str(row.get("Column_name") or row.get("column_name") or "").lower()
        seq = int(row.get("Seq_in_index") or row.get("seq_in_index") or 0)
        if key_name and col_name and seq >= 1:
            by_name.setdefault(key_name, {})[seq] = col_name
    expected = tuple(c.lower() for c in leading_columns)
    for key_name, seq_map in by_name.items():
        ordered = tuple(seq_map.get(i + 1, "") for i in range(len(expected)))
        if ordered == expected:
            return key_name
    return None


def _default_mysql_probe(
    mysql_cfg: dict[str, Any],
    databases: dict[str, str],
    *,
    discover_samples: bool,
) -> dict[str, Any]:
    import pymysql

    host = str(mysql_cfg.get("host") or "").strip()
    port = int(mysql_cfg.get("port") or 3306)
    user = str(mysql_cfg.get("user") or "").strip()
    password = str(mysql_cfg.get("password") or "")
    if not host or not user or not password:
        return {"connected": False, "error": "MYSQL_CREDENTIALS_MISSING"}
    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            charset="utf8mb4",
            connect_timeout=5,
            read_timeout=10,
            write_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
    except Exception as exc:
        return {"connected": False, "error": type(exc).__name__}

    client_host = "%"
    table_grants: dict[str, dict[str, bool]] = {}
    table_errors: dict[str, dict[str, int | str]] = {}
    indexes: dict[str, list[dict[str, Any]]] = {}
    samples: dict[str, list[dict[str, Any]]] = {
        "orders": [],
        "offline_devices": [],
        "consumer_orders": [],
    }
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT CURRENT_USER() AS cu")
                cu_row = cur.fetchone() or {}
                cu_val = str(cu_row.get("cu") or "")
                if "@" in cu_val:
                    client_host = cu_val.rsplit("@", 1)[1].strip() or "%"
            except Exception:
                pass

            for db_key, tables in REQUIRED_MYSQL_TABLES.items():
                db_name = _validate_ident(databases[db_key], f"database[{db_key}]")
                table_grants[db_key] = {}
                table_errors[db_key] = {}
                for tbl in tables:
                    _validate_ident(tbl, "table")
                    try:
                        cur.execute(f"SELECT 1 FROM `{db_name}`.`{tbl}` LIMIT 0")
                        cur.fetchall()
                        table_grants[db_key][tbl] = True
                    except Exception as exc:
                        table_grants[db_key][tbl] = False
                        code = exc.args[0] if getattr(exc, "args", None) else type(exc).__name__
                        table_errors[db_key][tbl] = code

            charging_db = _validate_ident(databases["charging"], "charging_database")
            for _, tbl, _, _ in REQUIRED_MYSQL_INDEXES:
                if table_grants.get("charging", {}).get(tbl):
                    try:
                        cur.execute(f"SHOW INDEX FROM `{charging_db}`.`{tbl}`")
                        indexes[tbl] = list(cur.fetchall())
                    except Exception:
                        indexes[tbl] = []

            if discover_samples and table_grants.get("charging", {}).get("ch_order_info"):
                try:
                    cur.execute(
                        f"SELECT order_no, device_id, status, created_time "
                        f"FROM `{charging_db}`.`ch_order_info` "
                        "WHERE order_no IS NOT NULL AND order_no != '' "
                        "ORDER BY created_time DESC LIMIT 3"
                    )
                    samples["orders"] = [
                        {
                            "order_no": str(r.get("order_no") or ""),
                            "device_id": str(r.get("device_id") or ""),
                            "status": r.get("status"),
                            "created_time": str(r.get("created_time") or ""),
                        }
                        for r in cur.fetchall()
                    ]
                except Exception:
                    pass
            if discover_samples and table_grants.get("charging", {}).get("iot_device_offline_monitor"):
                try:
                    cur.execute(
                        f"SELECT device_id, MAX(offline_time) AS latest_offline_time, "
                        "COUNT(*) AS event_count "
                        f"FROM `{charging_db}`.`iot_device_offline_monitor` "
                        "WHERE device_id IS NOT NULL AND device_id != '' "
                        "GROUP BY device_id ORDER BY latest_offline_time DESC LIMIT 3"
                    )
                    samples["offline_devices"] = [
                        {
                            "device_id": str(r.get("device_id") or ""),
                            "latest_offline_time": str(r.get("latest_offline_time") or ""),
                            "event_count": int(r.get("event_count") or 0),
                        }
                        for r in cur.fetchall()
                    ]
                except Exception:
                    pass
            if discover_samples and table_grants.get("charging", {}).get("ch_order_info"):
                try:
                    cur.execute(
                        f"SELECT order_no, user_id, status, created_time "
                        f"FROM `{charging_db}`.`ch_order_info` "
                        "WHERE user_id IS NOT NULL AND user_id != '' AND order_no IS NOT NULL "
                        "ORDER BY created_time DESC LIMIT 3"
                    )
                    samples["consumer_orders"] = [
                        {
                            "order_no": str(r.get("order_no") or ""),
                            "user_id_suffix": str(r.get("user_id") or "")[-4:],
                            "status": r.get("status"),
                            "created_time": str(r.get("created_time") or ""),
                        }
                        for r in cur.fetchall()
                    ]
                except Exception:
                    pass
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    return {
        "connected": True,
        "client_host": client_host,
        "table_grants": table_grants,
        "table_errors": table_errors,
        "indexes": indexes,
        "samples": samples,
    }


def _default_tsdata_probe(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"url": url, "reachable": False, "status": 0, "reason": "INVALID_URL"}
    probe_url = url.rstrip("/") + "/api/v1/health"
    req = Request(probe_url, method="GET")
    try:
        with urlopen(req, timeout=4) as resp:
            return {"url": url, "reachable": True, "status": int(resp.status)}
    except HTTPError as exc:
        # 任何非 502/503/504 的 HTTP 响应（含 401/404/405）均证明端口与服务进程存活。
        alive = exc.code not in {502, 503, 504}
        return {"url": url, "reachable": alive, "status": int(exc.code)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "reachable": False, "status": 0, "reason": type(exc).__name__}


def _default_redis_probe(redis_cfg: dict[str, Any]) -> dict[str, Any]:
    host = str(redis_cfg.get("host") or "").strip()
    port = int(redis_cfg.get("port") or 6379)
    password = str(redis_cfg.get("password") or "") or None
    db = int(redis_cfg.get("db") or 0)
    if not host:
        return {"reachable": False, "reason": "REDIS_HOST_MISSING"}
    try:
        import redis

        client = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            socket_connect_timeout=4,
            socket_timeout=4,
        )
        ok = bool(client.ping())
        client.close()
        return {"reachable": ok}
    except Exception as exc:
        return {"reachable": False, "reason": type(exc).__name__}


def _default_upms_tenants_probe(company_gateway_url: str) -> dict[str, Any]:
    parsed = urlsplit(company_gateway_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"reachable": False, "tenants": [], "reason": "INVALID_URL"}
    target = company_gateway_url.rstrip("/") + "/upms/tenant/list"
    req = Request(target, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=6) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            raw_list = body.get("data") if isinstance(body, dict) else None
            tenants = []
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    tid = str(item.get("id") or item.get("tenantId") or "").strip()
                    name = str(item.get("name") or "").strip()
                    if tid:
                        tenants.append(
                            {
                                "id": tid,
                                "name": name or tid,
                                "code": str(item.get("code") or ""),
                                "status": str(item.get("status") or ""),
                            }
                        )
            return {"reachable": True, "status": int(resp.status), "tenants": tenants}
    except HTTPError as exc:
        return {"reachable": False, "status": int(exc.code), "tenants": []}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {"reachable": False, "status": 0, "tenants": [], "reason": type(exc).__name__}


def run_data_preflight(
    env_values: dict[str, str | None],
    *,
    company_gateway_url: str = "",
    tsdata_fallback_urls: tuple[str, ...] = DEFAULT_TSDATA_LOOPBACK_CANDIDATES,
    discover_samples: bool = False,
    mysql_probe: Callable[..., dict[str, Any]] | None = None,
    tsdata_probe: Callable[[str], dict[str, Any]] | None = None,
    redis_probe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    upms_tenants_probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行全量只读数据面预检，返回零秘密的结构化体检单与受限修复计划。"""

    def val(key: str, default: str = "") -> str:
        return str(env_values.get(key) or default).strip()

    mysql_user = val("AIOPS_MYSQL_USER", "aiops_ro")
    databases = {
        "charging": val("AIOPS_MYSQL_DATABASE", "cloud_charging_pile"),
        "mall": val("AIOPS_MYSQL_MALL_DATABASE", "qumall_mall"),
        "upms": val("AIOPS_MYSQL_UPMS_DATABASE", "qumall_upms"),
    }
    mysql_cfg = {
        "host": val("AIOPS_MYSQL_HOST"),
        "port": int(val("AIOPS_MYSQL_PORT", "3306") or "3306"),
        "user": mysql_user,
        "password": val("AIOPS_MYSQL_PASSWORD"),
    }
    redis_cfg = {
        "host": val("AIOPS_REDIS_HOST"),
        "port": int(val("AIOPS_REDIS_PORT", "6379") or "6379"),
        "password": val("AIOPS_REDIS_PASSWORD"),
        "db": int(val("AIOPS_REDIS_DB", "0") or "0"),
    }
    configured_tsdata = val("AIOPS_GATEWAY_OPERATOR_TSDATA_URL")
    effective_gateway_url = company_gateway_url.strip() or val("AIOPS_UPMS_BASE_URL")
    if effective_gateway_url.endswith("/upms"):
        effective_gateway_url = effective_gateway_url[:-5]

    checks: list[PreflightCheck] = []
    remediations: list[RemediationAction] = []
    discovered: dict[str, Any] = {
        "tenants": [],
        "healthy_tsdata_url": None,
        "sample_candidates": {"orders": [], "offline_devices": [], "consumer_orders": []},
    }

    # 1. MySQL 连通性、最小只读权限矩阵与必选索引
    m_res = (mysql_probe or _default_mysql_probe)(mysql_cfg, databases, discover_samples=discover_samples)
    if not m_res.get("connected"):
        checks.append(
            PreflightCheck(
                id="mysql-connectivity",
                category="mysql",
                state="blocked",
                summary="MySQL 只读连接失败，请核对内网地址、端口与 aiops_ro 凭据。",
                detail={"error": str(m_res.get("error") or "CONNECT_FAILED")},
            )
        )
    else:
        checks.append(
            PreflightCheck(
                id="mysql-connectivity",
                category="mysql",
                state="passed",
                summary="MySQL 只读连接正常。",
                detail={"client_host": str(m_res.get("client_host") or "%")},
            )
        )
        client_host = str(m_res.get("client_host") or "%")
        table_grants = m_res.get("table_grants") or {}
        for db_key, label in (
            ("charging", "充电业务库（cloud_charging_pile）"),
            ("mall", "商城订单库（qumall_mall）"),
            ("upms", "UPMS 平台身份库（qumall_upms）"),
        ):
            grants_map = table_grants.get(db_key) or {}
            missing = [t for t in REQUIRED_MYSQL_TABLES[db_key] if not grants_map.get(t)]
            if not missing:
                checks.append(
                    PreflightCheck(
                        id=f"mysql-grants-{db_key}",
                        category="mysql",
                        state="passed",
                        summary=f"{label}最小只读权限齐全。",
                        detail={"database": databases[db_key], "tables": list(REQUIRED_MYSQL_TABLES[db_key])},
                    )
                )
            else:
                rem_id: str | None = None
                if db_key == "upms":
                    rem_id = "mysql_upms_select_grants"
                    sqls = build_upms_grant_sql(
                        mysql_user, client_host, missing, upms_database=databases["upms"]
                    )
                    remediations.append(
                        RemediationAction(
                            id=rem_id,
                            kind="mysql_grant",
                            title="补齐 UPMS 三张身份表的最小只读 SELECT 权限",
                            description=(
                                "正式公网 /v1/assistant/questions 需通过 aiops_ro 只读解析平台角色，"
                                "缺失将导致公网接口返回 503 PLATFORM_UNAVAILABLE。"
                            ),
                            auto_fixable=True,
                            risk_level="low",
                            sql_statements=sqls,
                        )
                    )
                checks.append(
                    PreflightCheck(
                        id=f"mysql-grants-{db_key}",
                        category="mysql",
                        state="blocked",
                        summary=f"{label}缺少只读表权限：{', '.join(missing)}。",
                        detail={"database": databases[db_key], "missing_tables": missing},
                        remediation_id=rem_id,
                    )
                )

        indexes_map = m_res.get("indexes") or {}
        missing_idx_specs: list[tuple[str, str, str, tuple[str, ...]]] = []
        for db_key, tbl, idx_name, cols in REQUIRED_MYSQL_INDEXES:
            if not (table_grants.get(db_key) or {}).get(tbl):
                continue
            matched = has_leading_index(indexes_map.get(tbl) or [], cols)
            if matched:
                checks.append(
                    PreflightCheck(
                        id=f"mysql-index-{tbl}",
                        category="mysql",
                        state="passed",
                        summary=f"表 {tbl} 必选查询索引已就绪（命中 {matched}）。",
                        detail={"table": tbl, "matched_index": matched, "columns": list(cols)},
                    )
                )
            else:
                missing_idx_specs.append((db_key, tbl, idx_name, cols))
                checks.append(
                    PreflightCheck(
                        id=f"mysql-index-{tbl}",
                        category="mysql",
                        state="blocked",
                        summary=f"表 {tbl} 缺少复合索引 ({', '.join(cols)})，设备离线/订单诊断将被安全拦截。",
                        detail={"table": tbl, "expected_index": idx_name, "columns": list(cols)},
                        remediation_id="mysql_required_indexes",
                    )
                )
        if missing_idx_specs:
            remediations.append(
                RemediationAction(
                    id="mysql_required_indexes",
                    kind="mysql_index",
                    title="在线无锁补建离线/订单查询索引",
                    description="使用 ALGORITHM=INPLACE, LOCK=NONE 在线建索引，不锁业务表。",
                    auto_fixable=True,
                    risk_level="low",
                    sql_statements=build_missing_index_sql(
                        missing_idx_specs, charging_database=databases["charging"]
                    ),
                )
            )
        if discover_samples and isinstance(m_res.get("samples"), dict):
            discovered["sample_candidates"] = m_res["samples"]

    # 2. TSData 主备通路探测（自动发现本机直连回环端口）
    ts_probe = tsdata_probe or _default_tsdata_probe
    ts_candidates: list[str] = []
    if configured_tsdata:
        ts_candidates.append(configured_tsdata)
    for fb in tsdata_fallback_urls:
        if fb and fb not in ts_candidates:
            ts_candidates.append(fb)

    if not ts_candidates:
        checks.append(
            PreflightCheck(
                id="tsdata-connectivity",
                category="tsdata",
                state="blocked",
                summary="未配置时序服务地址（AIOPS_GATEWAY_OPERATOR_TSDATA_URL）。",
            )
        )
    else:
        primary_res = ts_probe(ts_candidates[0])
        if primary_res.get("reachable"):
            discovered["healthy_tsdata_url"] = ts_candidates[0]
            checks.append(
                PreflightCheck(
                    id="tsdata-connectivity",
                    category="tsdata",
                    state="passed",
                    summary="时序服务（tsdata）主地址可达。",
                    detail={"url": ts_candidates[0], "status": primary_res.get("status", 0)},
                )
            )
        else:
            healthy_fb: dict[str, Any] | None = None
            for fb_url in ts_candidates[1:]:
                fb_res = ts_probe(fb_url)
                if fb_res.get("reachable"):
                    healthy_fb = fb_res
                    break
            if healthy_fb is not None:
                fb_url = str(healthy_fb["url"])
                discovered["healthy_tsdata_url"] = fb_url
                remediations.append(
                    RemediationAction(
                        id="tsdata_switch_loopback",
                        kind="tsdata_url",
                        title="切换时序服务至本机直连回环端口",
                        description=(f"配置的主地址不可达，但自动探测到本机回环时序服务 {fb_url} 可用。"),
                        auto_fixable=True,
                        risk_level="low",
                        suggested_config={"AIOPS_GATEWAY_OPERATOR_TSDATA_URL": fb_url},
                    )
                )
                checks.append(
                    PreflightCheck(
                        id="tsdata-connectivity",
                        category="tsdata",
                        state="blocked",
                        summary=f"时序服务主地址不可达，已发现可用本机回环地址 {fb_url}。",
                        detail={
                            "configured_url": ts_candidates[0],
                            "healthy_fallback_url": fb_url,
                        },
                        remediation_id="tsdata_switch_loopback",
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        id="tsdata-connectivity",
                        category="tsdata",
                        state="blocked",
                        summary="时序服务主地址与本机回环候选均不可达。",
                        detail={"probed_urls": ts_candidates},
                    )
                )

    # 3. Redis 连通性
    r_res = (redis_probe or _default_redis_probe)(redis_cfg)
    checks.append(
        PreflightCheck(
            id="redis-connectivity",
            category="redis",
            state="passed" if r_res.get("reachable") else "blocked",
            summary="Redis 连通正常。" if r_res.get("reachable") else "Redis 连接失败。",
            detail={} if r_res.get("reachable") else {"reason": str(r_res.get("reason") or "FAILED")},
        )
    )

    # 4. 公司 UPMS 租户清单发现（仅当提供 company_gateway_url 时检查）
    if effective_gateway_url:
        u_res = (upms_tenants_probe or _default_upms_tenants_probe)(effective_gateway_url)
        tenants = u_res.get("tenants") or []
        discovered["tenants"] = tenants
        if u_res.get("reachable"):
            checks.append(
                PreflightCheck(
                    id="upms-tenant-discovery",
                    category="upms",
                    state="passed" if tenants else "warning",
                    summary=f"已自动发现 {len(tenants)} 个客户租户。"
                    if tenants
                    else "UPMS 可达但租户列表为空。",
                    detail={"tenant_count": len(tenants)},
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    id="upms-tenant-discovery",
                    category="upms",
                    state="warning",
                    summary="未能通过公司网关拉取租户列表，请确认 API 域名或网络可达。",
                    detail={"status": int(u_res.get("status") or 0)},
                )
            )

    overall = "blocked" if any(c.state == "blocked" for c in checks) else "passed"
    return {
        "schema": "aiops-data-preflight/1",
        "state": overall,
        "ready": overall == "passed",
        "checks": [c.to_dict() for c in checks],
        "remediations": [r.to_dict() for r in remediations],
        "discovered": discovered,
    }


def run_data_preflight_from_file(
    config_file: Path,
    *,
    company_gateway_url: str = "",
    tsdata_fallback_urls: tuple[str, ...] = DEFAULT_TSDATA_LOOPBACK_CANDIDATES,
    discover_samples: bool = False,
) -> dict[str, Any]:
    """从受控 ``0600`` 环境文件加载配置并执行数据面预检；绝不回显文件内容或凭据。"""
    from aiops_diagnostics.common.private_files import validate_private_file

    validate_private_file(config_file)
    values = dotenv_values(config_file, interpolate=False)
    return run_data_preflight(
        dict(values),
        company_gateway_url=company_gateway_url,
        tsdata_fallback_urls=tsdata_fallback_urls,
        discover_samples=discover_samples,
    )
