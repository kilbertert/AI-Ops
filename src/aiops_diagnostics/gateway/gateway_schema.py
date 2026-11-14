"""Gateway 的 28 张表、已知历史追加与版本治理的唯一权威（ADR 0244）。

导入没有 I/O；普通构造仅检查版本。完整结构检查用于启动和运维。
不重建历史表、不猜权限快照、不删除额外表；MappingStore 使用独立数据库。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.gateway import gateway_db

VERSION = 2

# 列声明同时驱动 CREATE、白名单 ADD 与结构判据；不维护第二套 schema。
TABLES = {
    "consumer_knowledge_grants": (
        {
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "knowledge_base_id": "TEXT NOT NULL",
            "granted_by": "TEXT NOT NULL",
            "granted_at": "REAL NOT NULL",
            "revoked_at": "REAL",
        },
        ("PRIMARY KEY (tenant_id, environment, knowledge_base_id)",),
    ),
    "consumer_endpoints": (
        {
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "base_url": "TEXT NOT NULL",
            "registered_by": "TEXT NOT NULL",
            "registered_at": "REAL NOT NULL",
        },
        ("PRIMARY KEY (tenant_id, environment)",),
    ),
    "execution_node_capabilities": (
        {
            "environment": "TEXT NOT NULL",
            "node_id": "TEXT NOT NULL",
            "body": "TEXT NOT NULL",
            "observed_at": "REAL NOT NULL",
        },
        ("PRIMARY KEY(environment,node_id)",),
    ),
    "agent_runs": (
        {
            "run_id": "TEXT PRIMARY KEY",
            "scope_fingerprint": "TEXT NOT NULL",
            "scope_json": "TEXT NOT NULL",
            "order_no": "TEXT NOT NULL",
            "operation": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "app_id": "TEXT NOT NULL",
            "agent_id": "TEXT NOT NULL",
            "expected_node_id": "TEXT NOT NULL",
            "state": "TEXT NOT NULL",
            "reason": "TEXT",
            "snapshot_json": "TEXT",
            "actual_node_id": "TEXT",
            "node_build": "TEXT",
            "gateway_version": "TEXT NOT NULL",
            "created_at": "TEXT NOT NULL",
            "expires_at": "TEXT NOT NULL",
            "claimed_at": "TEXT",
            "completed_at": "TEXT",
            "qa_id": "TEXT",
            "chat_binding": "TEXT",
            "language": "TEXT",
            "expected_agent_version": "TEXT",
            "observed_agent_version": "TEXT",
            "exec_config_state": "TEXT",
            "execution_manifest": "TEXT",
            "route_revision": "INTEGER NOT NULL DEFAULT 0",
            "publication_app_id": "TEXT NOT NULL DEFAULT ''",
            "trace_json": "TEXT",
            "tool_summaries_json": "TEXT",
        },
        (),
    ),
    "chat_data_grants": (
        {
            "grant_id": "TEXT PRIMARY KEY",
            "token_hash": "TEXT NOT NULL UNIQUE",
            "scope_fingerprint": "TEXT NOT NULL",
            "order_no": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "qa_id": "TEXT NOT NULL",
            "workflow_id": "TEXT NOT NULL",
            "state": "TEXT NOT NULL",
            "created_at": "TEXT NOT NULL",
            "expires_at": "TEXT NOT NULL",
            "consumed_at": "TEXT",
        },
        (),
    ),
    "chat_route_bindings": (
        {
            "environment": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "entry": "TEXT NOT NULL",
            "scenario": "TEXT NOT NULL",
            "engine": "TEXT NOT NULL",
            "agent_id": "TEXT NOT NULL",
            "agent_version_no": "INTEGER NOT NULL",
            "app_id": "TEXT NOT NULL DEFAULT ''",
            "workflow_id": "TEXT NOT NULL DEFAULT ''",
            "workflow_key_ref": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT 'active'",
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "updated_by": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL",
            "node_config_file": "TEXT NOT NULL DEFAULT ''",
        },
        ("PRIMARY KEY (environment, tenant_id, entry, scenario)",),
    ),
    "agent_run_metrics": (
        {
            "metric_id": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "route_type": "TEXT NOT NULL",
            "outcome": "TEXT NOT NULL",
            "agent_id": "TEXT",
            "agent_version_key": "TEXT",
            "conversation_id": "TEXT",
            "retrieval_status": "TEXT NOT NULL DEFAULT ''",
            "searches": "INTEGER NOT NULL DEFAULT 0",
            "media_count": "INTEGER NOT NULL DEFAULT 0",
            "duration_ms": "INTEGER",
            "token_count": "INTEGER NOT NULL DEFAULT 0",
            "error_code": "TEXT",
            "created_at": "TEXT NOT NULL",
        },
        (),
    ),
    "health_report_jobs": (
        {
            "job_id": "TEXT PRIMARY KEY",
            "scope_fingerprint": "TEXT NOT NULL",
            "order_no": "TEXT NOT NULL",
            "rule_version": "TEXT NOT NULL",
            "status": "TEXT NOT NULL",
            "language": "TEXT NOT NULL DEFAULT 'zh'",
            "report_json": "TEXT",
            "error_code": "TEXT",
            "error_message": "TEXT",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
            "deadline_at": "TEXT NOT NULL",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "expires_at": "TEXT",
            "tenant_id": "TEXT",
        },
        (),
    ),
    "standard_diagnoses": (
        {
            "diagnosis_id": "TEXT PRIMARY KEY",
            "scope_fingerprint": "TEXT NOT NULL",
            "order_no": "TEXT NOT NULL",
            "question": "TEXT NOT NULL",
            "indicator_code": "TEXT",
            "language": "TEXT NOT NULL DEFAULT 'zh'",
            "internal_run_id": "TEXT",
            "status": "TEXT NOT NULL",
            "result_json": "TEXT",
            "error_code": "TEXT",
            "error_message": "TEXT",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
            "deadline_at": "TEXT NOT NULL",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "expires_at": "TEXT",
            "kind": "TEXT NOT NULL DEFAULT 'order'",
            "site_ids_json": "TEXT",
            "window_start": "TEXT",
            "window_end": "TEXT",
            "rule_version": "TEXT",
            "card": "TEXT",
            "trial_draft_fingerprint": "TEXT",
            "trial_initiator": "TEXT",
            "trial_source_app_id": "TEXT",
            "trial_managed_draft": "INTEGER NOT NULL DEFAULT 0",
            "tenant_id": "TEXT",
        },
        (),
    ),
    "assistant_questions": (
        {
            "qa_id": "TEXT PRIMARY KEY",
            "scope_fingerprint": "TEXT NOT NULL",
            "question": "TEXT NOT NULL",
            "internal_run_id": "TEXT",
            "status": "TEXT NOT NULL",
            "result_json": "TEXT",
            "error_code": "TEXT",
            "error_message": "TEXT",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
            "deadline_at": "TEXT NOT NULL",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "expires_at": "TEXT",
            "tenant_id": "TEXT",
        },
        (),
    ),
    "publish_audit_ledger": (
        {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "at": "TEXT NOT NULL",
            "app_id": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL DEFAULT ''",
            "business_entry": "TEXT NOT NULL DEFAULT ''",
            "agent_name": "TEXT NOT NULL DEFAULT ''",
            "company_user_id": "TEXT NOT NULL DEFAULT ''",
            "company_username": "TEXT NOT NULL DEFAULT ''",
            "dify_account_id": "TEXT NOT NULL DEFAULT ''",
            "version_no": "INTEGER",
            "action": "TEXT NOT NULL",
            "reason_code": "TEXT NOT NULL DEFAULT ''",
        },
        (),
    ),
    "consumer_copy_bundles": (
        {
            "tenant_id": "TEXT NOT NULL",
            "version": "INTEGER NOT NULL",
            "payload_json": "TEXT NOT NULL",
            "published_at": "TEXT NOT NULL",
            "published_by": "TEXT NOT NULL",
        },
        ("PRIMARY KEY (tenant_id, version)",),
    ),
    "knowledge_access_ledger": (
        {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "at": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "company_user_id": "TEXT NOT NULL DEFAULT ''",
            "ragflow_email": "TEXT NOT NULL DEFAULT ''",
            "outcome": "TEXT NOT NULL",
            "reason_code": "TEXT NOT NULL DEFAULT ''",
        },
        (),
    ),
    "knowledge_hop_tickets": (
        {
            "ticket": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "workspace_id": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL",
            "expires_at": "TEXT NOT NULL",
            "used_at": "TEXT",
        },
        (),
    ),
    "dify_knowledge_bindings": (
        {
            "knowledge_id": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "knowledge_base_ids": "TEXT NOT NULL",
            "display_name": "TEXT NOT NULL DEFAULT ''",
            "created_by": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL",
        },
        (),
    ),
    "ragflow_tenants": (
        {
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "ragflow_email": "TEXT NOT NULL",
            "credential_ciphertext": "TEXT NOT NULL DEFAULT ''",
            "model_checked_at": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
        },
        ("PRIMARY KEY (tenant_id, environment)",),
    ),
    "agents": (
        {
            "agent_id": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "name": "TEXT NOT NULL",
            "description": "TEXT NOT NULL",
            "status": "TEXT NOT NULL",
            "revision": "INTEGER NOT NULL",
            "config_json": "TEXT NOT NULL",
            "published_version": "INTEGER",
            "created_by": "TEXT NOT NULL",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
        },
        (),
    ),
    "agent_versions": (
        {
            "agent_id": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "version_no": "INTEGER NOT NULL",
            "snapshot_json": "TEXT NOT NULL",
            "published_by": "TEXT NOT NULL",
            "published_at": "TEXT NOT NULL",
        },
        (
            "PRIMARY KEY(agent_id, version_no)",
            "FOREIGN KEY(agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE",
        ),
    ),
    "shortcuts": (
        {
            "shortcut_id": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "business_entry": "TEXT NOT NULL",
            "code": "TEXT NOT NULL",
            "intent": "TEXT NOT NULL",
            "requires_order": "INTEGER NOT NULL",
            "sort_order": "INTEGER NOT NULL",
            "status": "TEXT NOT NULL",
            "revision": "INTEGER NOT NULL",
            "fields_json": "TEXT NOT NULL",
            "published_version": "INTEGER",
            "kind": "TEXT NOT NULL DEFAULT 'button'",
            "image_url": "TEXT",
            "created_by": "TEXT NOT NULL",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
        },
        ("UNIQUE(tenant_id, business_entry, code)",),
    ),
    "shortcut_versions": (
        {
            "shortcut_id": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "version_no": "INTEGER NOT NULL",
            "snapshot_json": "TEXT NOT NULL",
            "published_by": "TEXT NOT NULL",
            "published_at": "TEXT NOT NULL",
        },
        (
            "PRIMARY KEY(shortcut_id, version_no)",
            "FOREIGN KEY(shortcut_id) REFERENCES shortcuts(shortcut_id) ON DELETE CASCADE",
        ),
    ),
    "conversations": (
        {
            "conversation_id": "TEXT PRIMARY KEY",
            "scope_fingerprint": "TEXT NOT NULL",
            "business_entry": "TEXT NOT NULL",
            "agent_version_key": "TEXT NOT NULL",
            "active_order_no": "TEXT",
            "generating_since": "TEXT",
            "generating_turn_no": "INTEGER",
            "last_turn_no": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
            "expires_at": "TEXT NOT NULL",
            "operator_binding": "TEXT NOT NULL DEFAULT ''",
        },
        (),
    ),
    "conversation_turns": (
        {
            "turn_id": "TEXT PRIMARY KEY",
            "conversation_id": "TEXT NOT NULL",
            "turn_no": "INTEGER NOT NULL",
            "kind": "TEXT NOT NULL",
            "question": "TEXT NOT NULL",
            "answer_json": "TEXT",
            "token_count": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL",
            "status_qa_id": "TEXT",
        },
        ("UNIQUE (conversation_id, turn_no)",),
    ),
    "chat_dify_runs": (
        {
            "run_id": "TEXT PRIMARY KEY",
            "qa_id": "TEXT NOT NULL",
            "scope_fingerprint": "TEXT NOT NULL",
            "scope_json": "TEXT NOT NULL",
            "binding_key": "TEXT NOT NULL DEFAULT ''",
            "order_no": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "tenant_id": "TEXT NOT NULL",
            "app_id": "TEXT NOT NULL",
            "workflow_id": "TEXT NOT NULL",
            "workflow_run_id": "TEXT",
            "task_id": "TEXT",
            "grant_id": "TEXT NOT NULL",
            "state": "TEXT NOT NULL",
            "reason": "TEXT",
            "created_at": "TEXT NOT NULL",
            "expires_at": "TEXT NOT NULL",
            "completed_at": "TEXT",
            "snapshot_json": "TEXT",
            "agent_version_no": "INTEGER NOT NULL DEFAULT 0",
            "kind": "TEXT NOT NULL DEFAULT 'chat'",
            "conversation_id": "TEXT NOT NULL DEFAULT ''",
            "upstream_conversation_id": "TEXT NOT NULL DEFAULT ''",
            "message_id": "TEXT NOT NULL DEFAULT ''",
            "binding_digest": "TEXT NOT NULL DEFAULT ''",
            "has_photo": "INTEGER NOT NULL DEFAULT 0",
            "data_kind": "TEXT NOT NULL DEFAULT 'status'",
            "context_json": "TEXT",
            "reply_source": "TEXT",
            "reject_detail": "TEXT",
            "language": "TEXT NOT NULL DEFAULT 'zh'",
            "route_revision": "INTEGER NOT NULL DEFAULT 0",
        },
        (),
    ),
    "operator_apps": (
        {
            "app_id": "TEXT PRIMARY KEY",
            "tenant_id": "TEXT NOT NULL",
            "workspace_id": "TEXT NOT NULL",
            "agent_name": "TEXT NOT NULL",
            "profile_id": "TEXT NOT NULL",
            "created_by": "TEXT NOT NULL",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "entry": "TEXT NOT NULL DEFAULT 'operator'",
            "scenario": "TEXT NOT NULL DEFAULT 'operator_order_diagnosis'",
            "environment": "TEXT NOT NULL DEFAULT ''",
            "targets_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        (),
    ),
    "operator_setup_jobs": (
        {
            "id": "TEXT PRIMARY KEY",
            "actor": "TEXT NOT NULL",
            "request_key": "TEXT NOT NULL",
            "request_json": "TEXT NOT NULL",
            "state": "TEXT NOT NULL",
            "step": "TEXT NOT NULL",
            "resources_json": "TEXT NOT NULL DEFAULT '{}'",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "lease_until": "REAL NOT NULL DEFAULT 0",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "REAL NOT NULL",
        },
        ("UNIQUE(actor, request_key)",),
    ),
    "dify_app_registry": (
        {
            "tenant_id": "TEXT NOT NULL",
            "business_entry": "TEXT NOT NULL",
            "app_id": "TEXT NOT NULL",
            "agent_name": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL",
        },
        ("PRIMARY KEY (tenant_id, business_entry)",),
    ),
    "service_key_ops": (
        {
            "app_id": "TEXT PRIMARY KEY",
            "workspace_id": "TEXT NOT NULL",
            "before_json": "TEXT NOT NULL",
            "stage": "TEXT NOT NULL",
            "updated_at": "REAL NOT NULL",
        },
        (),
    ),
    "dify_workspaces": (
        {
            "tenant_id": "TEXT NOT NULL",
            "environment": "TEXT NOT NULL",
            "workspace_id": "TEXT NOT NULL DEFAULT ''",
            "workspace_name": "TEXT NOT NULL DEFAULT ''",
            "setup_state": "TEXT NOT NULL",
            "lease_owner": "TEXT NOT NULL DEFAULT ''",
            "lease_expires_at": "TEXT",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
        },
        ("PRIMARY KEY (tenant_id, environment)",),
    ),
}

INDEXES = {
    "idx_agent_runs_scope": ("agent_runs", "scope_fingerprint, created_at DESC"),
    "idx_agent_runs_qa": ("agent_runs", "qa_id"),
    "idx_chat_data_grants_qa": ("chat_data_grants", "qa_id"),
    "idx_agent_metrics_tenant_created": ("agent_run_metrics", "tenant_id, created_at DESC"),
    "idx_agent_metrics_agent": ("agent_run_metrics", "agent_id, created_at DESC"),
    "idx_agent_metrics_created": ("agent_run_metrics", "created_at"),
    "idx_health_jobs_reuse": (
        "health_report_jobs",
        "scope_fingerprint, order_no, rule_version, created_at DESC",
    ),
    "idx_standard_diagnoses_scope_created": ("standard_diagnoses", "scope_fingerprint, created_at DESC"),
    "idx_assistant_questions_scope_created": ("assistant_questions", "scope_fingerprint, created_at DESC"),
    "idx_publish_audit_at": ("publish_audit_ledger", "at DESC"),
    "idx_publish_audit_app": ("publish_audit_ledger", "app_id, at DESC"),
    "idx_knowledge_access_tenant": ("knowledge_access_ledger", "tenant_id, at DESC"),
    "idx_knowledge_hop_expiry": ("knowledge_hop_tickets", "expires_at"),
    "idx_dify_knowledge_tenant": ("dify_knowledge_bindings", "tenant_id, created_at DESC"),
    "idx_standard_diagnoses_scope_kind_created": (
        "standard_diagnoses",
        "scope_fingerprint, kind, created_at DESC",
    ),
    "idx_health_jobs_reuse_language": (
        "health_report_jobs",
        "scope_fingerprint, order_no, rule_version, language, created_at DESC",
    ),
    "idx_agents_tenant_created": ("agents", "tenant_id, created_at DESC"),
    "idx_shortcuts_listing": ("shortcuts", "tenant_id, business_entry, status, sort_order"),
    "idx_conversations_scope_created": ("conversations", "scope_fingerprint, created_at DESC"),
    "idx_conversation_turns_conversation": ("conversation_turns", "conversation_id, turn_no DESC"),
    "idx_chat_dify_runs_qa": ("chat_dify_runs", "qa_id"),
}

LEGACY_COLUMNS = {
    "consumer_knowledge_grants": ("revoked_at",),
    "chat_route_bindings": ("node_config_file",),
    "agent_runs": (
        "qa_id",
        "chat_binding",
        "expected_agent_version",
        "observed_agent_version",
        "exec_config_state",
        "execution_manifest",
        "route_revision",
        "publication_app_id",
        "language",
        "trace_json",
        "tool_summaries_json",
    ),
    "chat_dify_runs": (
        "snapshot_json",
        "binding_key",
        "scope_json",
        "kind",
        "conversation_id",
        "upstream_conversation_id",
        "message_id",
        "binding_digest",
        "has_photo",
        "data_kind",
        "context_json",
        "reply_source",
        "reject_detail",
        "language",
        "route_revision",
        "agent_version_no",
    ),
    "health_report_jobs": ("language", "tenant_id"),
    "standard_diagnoses": (
        "language",
        "kind",
        "site_ids_json",
        "window_start",
        "window_end",
        "rule_version",
        "card",
        "trial_draft_fingerprint",
        "trial_initiator",
        "trial_source_app_id",
        "trial_managed_draft",
        "tenant_id",
    ),
    "assistant_questions": ("tenant_id",),
    "shortcuts": ("kind", "image_url"),
    "conversations": ("generating_turn_no", "last_turn_no", "operator_binding"),
    "conversation_turns": ("status_qa_id",),
    "operator_apps": ("entry", "scenario", "environment", "targets_json"),
    "dify_workspaces": ("workspace_name",),
}


class SchemaError(ValueError):
    """结构不在已知合同内；消息只包含结构名称，不包含业务行。"""


def _version(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # 0 = 版本化之前的库，1..VERSION = 已知历史发布；缺的新列由 LEGACY_COLUMNS 放行、
    # ensure 补齐。只有更晚版本写出的库（或手工改坏的值）才拒绝——旧代码开新库必须 fail closed。
    if not 0 <= version <= VERSION:
        raise SchemaError(f"unsupported gateway schema version: {version}")
    return version


def _create(table: str, columns: dict[str, str]) -> str:
    definitions = [f"{name} {spec}" for name, spec in columns.items()]
    definitions.extend(TABLES[table][1])
    return f'CREATE TABLE "{table}" (' + ", ".join(definitions) + ")"


def _columns(conn: sqlite3.Connection, table: str) -> dict:
    # cid 是物理顺序，不是合同；xinfo 同时识别不允许的隐藏/生成列。
    return {row[1]: tuple(row)[2:] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')}


def _indexes(conn: sqlite3.Connection, table: str) -> dict:
    result = {}
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        name, unique, origin, partial = row[1:5]
        # 隐式索引名随表中约束顺序改变；其唯一性及键序才是合同。
        key = (
            name
            if origin == "c"
            else (
                origin,
                tuple(
                    (item[2], item[3], item[4], item[5])
                    for item in conn.execute(f'PRAGMA index_xinfo("{name}")')
                    if item[5]
                ),
            )
        )
        result[key] = (
            unique,
            origin,
            partial,
            tuple(
                (item[2], item[3], item[4], item[5])
                for item in conn.execute(f'PRAGMA index_xinfo("{name}")')
                if item[5]
            ),
        )
    return result


# 有界词法比对已知 CREATE TABLE 子句，拒绝 PRAGMA 看不到的 CHECK/冲突策略。
# 不解析任意 SQL、不重写用户 DDL；字符串逐字保留，标识符不区分大小写。
_SQL_TOKEN = re.compile(
    r"'([^']|'')*'|\"([^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]",
    re.S,
)


def _definitions(sql: str) -> Counter:
    tokens = []
    for match in _SQL_TOKEN.finditer(sql):
        token = match.group()
        if token.startswith(("--", "/*")):
            continue
        if token.startswith(('"', "`", "[")):
            token = token[1:-1]
        tokens.append(token if token.startswith("'") else token.upper())
    if tokens and tokens[-1] == ";":
        tokens.pop()
    opening = tokens.index("(")
    if tokens[:2] != ["CREATE", "TABLE"] or tokens[-1] != ")":
        raise SchemaError("unsupported table declaration")
    parts, part, depth = [], [], 0
    for token in tokens[opening + 1 : -1]:
        if token == "," and depth == 0:
            parts.append(tuple(part))
            part = []
        else:
            part.append(token)
            depth += (token == "(") - (token == ")")
            if depth < 0:
                raise SchemaError("unsupported table declaration")
    if depth:
        raise SchemaError("unsupported table declaration")
    parts.append(tuple(part))
    return Counter(parts)


def _validate(conn: sqlite3.Connection, *, legacy: bool) -> list[str]:
    objects = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall()
    actual = {r[1]: r[3] for r in objects if r[0] == "table"}
    if objects and not (actual.keys() & TABLES.keys()):
        raise SchemaError("not a gateway database")
    warnings = [f"extra table: {name}" for name in sorted(actual.keys() - TABLES.keys())]
    for kind, name, table, _ in objects:
        if name in TABLES and kind != "table":
            raise SchemaError(f"managed name collision: {name}")
        if name in INDEXES and (kind != "index" or table != INDEXES[name][0]):
            raise SchemaError(f"index name collision: {name}")
    if any(r[0] in ("trigger", "view") and (r[2] in TABLES or r[1] in TABLES) for r in objects):
        raise SchemaError("unexpected managed trigger or view")
    # SQLite itself interprets types, constraints and indexes; no second hand-written shape.
    with gateway_db.connection(Path(":memory:")) as expected:
        for table, (declared, _) in TABLES.items():
            if table not in actual:
                if legacy:
                    continue
                raise SchemaError(f"missing table: {table}")
            columns = _columns(conn, table)
            missing = declared.keys() - columns.keys()
            if columns.keys() - declared.keys() or (
                missing and (not legacy or missing - set(LEGACY_COLUMNS.get(table, ())))
            ):
                raise SchemaError(f"unknown or missing columns: {table}")
            selected = {k: v for k, v in declared.items() if k in columns}
            if table == "chat_dify_runs" and "scope_json" in columns and columns["scope_json"][2] == "''":
                selected["scope_json"] += " DEFAULT ''"
                warnings.append("legacy default: chat_dify_runs.scope_json = ''")
            sql = _create(table, selected)
            expected.execute(sql)
            if _columns(expected, table) != columns or _definitions(actual[table]) != _definitions(sql):
                raise SchemaError(f"column or constraint drift: {table}")

            def fk(db, table=table):
                return sorted(tuple(r) for r in db.execute(f'PRAGMA foreign_key_list("{table}")'))

            if fk(conn) != fk(expected):
                raise SchemaError(f"foreign key drift: {table}")
        for name, (table, fields) in INDEXES.items():
            sql = f'CREATE INDEX "{name}" ON "{table}" ({fields})'
            if table in actual:
                # A historical missing column may also make its index absent.
                try:
                    expected.execute(sql)
                except sqlite3.OperationalError:
                    if not legacy:
                        raise
        for table in actual.keys() & TABLES.keys():
            found, wanted = _indexes(conn, table), _indexes(expected, table)
            if any(key not in wanted or wanted[key] != value for key, value in found.items()) or (
                not legacy and found != wanted
            ):
                raise SchemaError(f"index drift: {table}")
    return warnings


def check(path: Path) -> dict:
    """只读检查现有库；版本低于当前的已知历史库返回可迁移结构报告，不创建文件或执行迁移。"""
    with gateway_db.connection(Path(path), readonly=True) as conn:
        conn.execute("BEGIN")
        version = _version(conn)
        warnings = _validate(conn, legacy=version < VERSION)
        return {
            "version": version,
            "current_version": VERSION,
            "managed_tables": len(TABLES),
            "warnings": warnings,
        }


def ensure(path: Path) -> None:
    """当前版只读快返；空库/已知历史库在同一写事务内升级。"""
    path = Path(path)
    if path.exists():
        with gateway_db.connection(path, readonly=True) as conn:
            if _version(conn) == VERSION:
                return
        check(path)  # 拒绝不认识的文件，连 WAL 属性也不碰。
    ensure_private_directory(path.parent)
    with gateway_db.initialize(path) as conn:
        if _version(conn) == VERSION:
            return  # 另一个进程已完成；必须在拿锁后重读。
        _validate(conn, legacy=True)
        for table, (columns, _) in TABLES.items():
            present = _columns(conn, table)
            if not present:
                conn.execute(_create(table, columns))
                continue
            for name in columns:
                if name in present:
                    continue
                spec = columns[name]
                if (table, name) == ("chat_dify_runs", "scope_json"):
                    spec += " DEFAULT ''"
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {name} {spec}')
        for name, (table, fields) in INDEXES.items():
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({fields})')
        _validate(conn, legacy=False)
        conn.execute(f"PRAGMA user_version={VERSION}")
    protect_private_file(path)


def startup(path: Path) -> dict:
    """服务/运维启动：升级后完整检查，同版本也不能跳过结构检查。"""
    ensure(path)
    return check(path)


def integrity(path: Path) -> None:
    """演练/切换前检查；拒绝时不输出包含业务键的 foreign_key_check 行。"""
    with gateway_db.connection(path, readonly=True) as conn:
        conn.execute("BEGIN")
        if [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise SchemaError("integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaError("foreign key check failed")


def rehearse(path: Path, backup: Path) -> dict:
    """保存一致原库备份，再迁移临时副本；绝不替换源库或覆盖已有备份。"""
    import tempfile

    backup = Path(backup)
    ensure_private_directory(backup.parent)
    # 独占创建以防误覆盖历史备份。失败留下备份供定位，不把失败当作可部署。
    with backup.open("xb"):
        pass
    protect_private_file(backup)
    with gateway_db.connection(path, readonly=True) as source, gateway_db.connection(backup) as target:
        source.backup(target)
    with tempfile.TemporaryDirectory(prefix="schema-rehearsal-", dir=backup.parent) as root:
        trial = Path(root) / "gateway.db"
        with gateway_db.connection(backup, readonly=True) as source, gateway_db.connection(trial) as target:
            source.backup(target)
        report = startup(trial)
        integrity(trial)
    return report
