"""引导期二次确认的受限数据库白名单修复执行器（ADR-0358 P1-2）。

安全红线（fail-closed）：
1. **严格白名单**：仅允许执行两类静态可审计语句：
   - 对 ``qumall_upms.{sys_user, sys_user_role, sys_role}`` 的单表最小 ``GRANT SELECT``；
   - 对 ``cloud_charging_pile.iot_device_offline_monitor``（``(device_id, offline_time)``）与
     ``cloud_charging_pile.ch_order_info``（``(device_id)``）的在线无锁建索引
     （必须显式带 ``ALGORITHM=INPLACE, LOCK=NONE``）。
   **严禁**执行任何白名单外的任意 SQL、多语句拼接、写业务数据、删改表或宽权限授权。
2. **强制显式二次确认**：必须携带 ``confirmed_by_operator=True`` 及与精确 SQL 集合绑定的
   ``confirmation_digest``（SHA-256），不匹配立即拒绝。
3. **执行后即时复验 + 0600 审计留痕**：执行完毕必须立刻以只读账号 ``aiops_ro`` 复验生效，
   并落盘零秘密的审计记录。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.onboarding.discovery import compute_remediation_confirmation_digest

ALLOWED_REMEDIATION_IDS = frozenset({"mysql_upms_select_grants", "mysql_required_indexes"})
ALLOWED_UPMS_TABLES = frozenset({"sys_user", "sys_user_role", "sys_role"})
ALLOWED_INDEX_SPECS: dict[tuple[str, str], str] = {
    ("iot_device_offline_monitor", "aiops_offline_device_time"): "(`device_id`, `offline_time`)",
    ("ch_order_info", "ch_order_info_device_id_index"): "(`device_id`)",
}


class RemediationRefused(ValueError):
    """受限数据库修复被白名单、二次确认或审计门禁拒绝时的稳定异常。"""


def _is_safe_ident(value: str) -> bool:
    return (
        bool(value) and len(value) <= 64 and all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in value)
    )


def _is_safe_mysql_host(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and all(ch.isascii() and (ch.isalnum() or ch in "._:%-") for ch in value)
    )


def _validate_single_grant_sql(stmt: str) -> bool:
    prefix = "GRANT SELECT ON `"
    if not stmt.startswith(prefix) or not stmt.endswith("';"):
        return False
    body = stmt[len(prefix) : -2]
    if "` TO '" not in body or "`.`" not in body:
        return False
    db_tbl, user_host = body.split("` TO '", 1)
    if "`" in user_host or "'; " in stmt:
        return False
    db_parts = db_tbl.split("`.`")
    uh_parts = user_host.split("'@'")
    if len(db_parts) != 2 or len(uh_parts) != 2:
        return False
    db_name, table_name = db_parts
    user_name, host_name = uh_parts
    if "`" in db_name or "`" in table_name:
        return False
    return (
        _is_safe_ident(db_name)
        and table_name in ALLOWED_UPMS_TABLES
        and _is_safe_ident(user_name)
        and _is_safe_mysql_host(host_name)
    )


def _validate_single_index_sql(stmt: str) -> bool:
    prefix = "ALTER TABLE `"
    suffix = ", ALGORITHM=INPLACE, LOCK=NONE;"
    if not stmt.startswith(prefix) or not stmt.endswith(suffix):
        return False
    body = stmt[len(prefix) : -len(suffix)]
    if "` ADD INDEX `" not in body or "`.`" not in body:
        return False
    db_tbl, idx_rest = body.split("` ADD INDEX `", 1)
    db_parts = db_tbl.split("`.`")
    if len(db_parts) != 2:
        return False
    db_name, table_name = db_parts
    if not _is_safe_ident(db_name) or "` (" not in idx_rest:
        return False
    idx_name, cols_tail = idx_rest.split("` ", 1)
    expected_cols = ALLOWED_INDEX_SPECS.get((table_name, idx_name))
    return expected_cols is not None and cols_tail == expected_cols


def validate_allowlisted_sql(sql_statements: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """逐条校验 SQL 严格落入静态白名单，拒绝任何多语句、注释或非允许 DDL/DML。"""
    if not sql_statements or len(sql_statements) > 10:
        raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_SQL_EMPTY_OR_TOO_MANY")
    cleaned: list[str] = []
    for raw in sql_statements:
        if not isinstance(raw, str):
            raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_SQL_FORBIDDEN")
        stmt = raw.strip()
        if "\n" in stmt or "\r" in stmt or "--" in stmt or "/*" in stmt or stmt.count(";") != 1:
            raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_SQL_FORBIDDEN")
        if not (_validate_single_grant_sql(stmt) or _validate_single_index_sql(stmt)):
            raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_SQL_FORBIDDEN")
        cleaned.append(stmt)
    return tuple(cleaned)


def execute_confirmed_db_remediation(
    *,
    environment: str,
    actor: str,
    remediation_ids: list[str] | tuple[str, ...],
    sql_statements: list[str] | tuple[str, ...],
    confirmed_by_operator: bool,
    confirmation_digest: str,
    executor: Callable[[tuple[str, ...]], None],
    reverify_runner: Callable[[], dict[str, Any]],
    audit_log_path: Path | None = None,
) -> dict[str, Any]:
    """经运维显式二次确认后，仅执行白名单 SQL 并立刻以 aiops_ro 复验生效。"""
    if confirmed_by_operator is not True:
        raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_CONFIRMATION_REQUIRED")
    rem_ids = [str(r) for r in remediation_ids]
    if not rem_ids or not set(rem_ids).issubset(ALLOWED_REMEDIATION_IDS):
        raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_ID_FORBIDDEN")

    validated_sql = validate_allowlisted_sql(sql_statements)
    expected_digest = compute_remediation_confirmation_digest(environment, rem_ids, validated_sql)
    if not isinstance(confirmation_digest, str) or confirmation_digest != expected_digest:
        raise RemediationRefused("ENVIRONMENT_DB_REMEDIATION_DIGEST_MISMATCH")

    executor(validated_sql)
    post_report = reverify_runner()
    verified_ready = bool(isinstance(post_report, dict) and post_report.get("ready"))

    audit_record = {
        "timestamp": time.time(),
        "environment": environment,
        "actor": str(actor),
        "remediation_ids": sorted(rem_ids),
        "executed_count": len(validated_sql),
        "confirmation_digest": expected_digest,
        "verified_after_fix": verified_ready,
    }
    if audit_log_path is not None:
        ensure_private_directory(audit_log_path.parent)
        with audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(audit_record, ensure_ascii=False, sort_keys=True) + "\n")
        protect_private_file(audit_log_path)

    return {
        "schema": "aiops-db-remediation-result/1",
        "environment": environment,
        "executed": True,
        "executed_count": len(validated_sql),
        "remediation_ids": sorted(rem_ids),
        "confirmation_digest": expected_digest,
        "verified_after_fix": verified_ready,
        "post_preflight": post_report,
    }
