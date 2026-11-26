"""执行环境迁移的冻结备份清单校验与逐项 CAS 恢复。

承接 ``SCHEMA``、``AdoptionError``、共用表结构/在飞作业/激活绑定预检及 ``_restore_from_backup``，
使 ``execution_adoption.py`` 单向依赖本模块并保持严格无环。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.gateway.gateway_store import JOB_TABLES

SCHEMA = "aiops-execution-environment-adoption/1"
_PREVIEW_PREFIX = "operator-trial:"
_REQUIRED_TABLES = frozenset(
    {
        "chat_route_bindings",
        "agents",
        "agent_versions",
        "operator_apps",
        "consumer_knowledge_grants",
        "conversations",
        "agent_runs",
        "chat_dify_runs",
        "operator_setup_jobs",
        *JOB_TABLES,
    }
)


class AdoptionError(ValueError):
    """稳定错误码的迁移拒绝；不向公开消息泄露密钥、路径或业务数据。"""


def _assert_schema_present(conn) -> None:
    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not _REQUIRED_TABLES.issubset(tables):
        raise AdoptionError("ADOPTION_SCHEMA_INCOMPLETE")


def _assert_no_inflight_work(conn) -> None:
    now_iso = format_stored_time(now_utc())
    checks: list[tuple[str, str, tuple[str, ...]]] = [
        (table, "status", ("queued", "running")) for table in JOB_TABLES
    ]
    checks += [
        ("chat_dify_runs", "state", ("queued", "issued", "running")),
        ("operator_setup_jobs", "state", ("pending", "running")),
    ]
    for table, column, states in checks:
        placeholders = ",".join("?" for _ in states)
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) LIMIT 1", states
        ).fetchone():
            raise AdoptionError("ADOPTION_INFLIGHT_WORK")
    # 沿用 agent_runs._expire_in 的过期判据（expires_at <= now 已过期，仅未过期行算在飞）。
    if conn.execute(
        "SELECT 1 FROM agent_runs WHERE state IN ('issued', 'running')"
        " AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
        (now_iso,),
    ).fetchone():
        raise AdoptionError("ADOPTION_INFLIGHT_WORK")
    if conn.execute("SELECT 1 FROM conversations WHERE generating_since IS NOT NULL LIMIT 1").fetchone():
        raise AdoptionError("ADOPTION_INFLIGHT_WORK")


def _prefix_clause(column: str, prefixes: tuple[str, ...]) -> tuple[str, list[Any]]:
    """构造逐字前缀匹配条件，避免 SQLite ``LIKE`` 把 ``_``/``%`` 当通配符。"""
    clause = " OR ".join(f"substr({column}, 1, ?) = ?" for _ in prefixes)
    params: list[Any] = []
    for prefix in prefixes:
        params.extend((len(prefix), prefix))
    return f"({clause})", params


def _operator_prefixes(routes: list[dict[str, Any]], environment: str) -> tuple[str, ...]:
    scenarios = {"operator_order_diagnosis"} | {
        str(r["scenario"]) for r in routes if r.get("entry") == "operator"
    }
    return tuple(sorted(f"{scenario}:{environment}:" for scenario in scenarios))


def _validate_consumer_snapshot(
    snapshot: Any,
    *,
    app_id: str,
    workflow_id: str,
    tenant_id: str,
    environment: str,
) -> None:
    """复用 ``chat_execution.validate_consumer_snapshot``，保证与真实加载器逐项一致。"""
    try:
        chat_execution.validate_consumer_snapshot(
            snapshot,
            app_id=app_id,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            environment=environment,
        )
    except chat_execution.ChatBindingError as exc:
        raise AdoptionError("ADOPTION_CONSUMER_SNAPSHOT_INVALID") from exc


def _validate_node_snapshot(snapshot: Any) -> str:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("node_manifest"), str):
        raise AdoptionError("ADOPTION_NODE_SNAPSHOT_INVALID")
    text = snapshot["node_manifest"]
    try:
        parsed = execution_manifest.from_text(text)
    except execution_manifest.ExecutionManifestError as exc:
        raise AdoptionError("ADOPTION_NODE_SNAPSHOT_INVALID") from exc
    if parsed is None:
        raise AdoptionError("ADOPTION_NODE_SNAPSHOT_INVALID")
    return text


def _load_snapshot(conn, agent_id: str, tenant_id: str, version_no: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT snapshot_json
          FROM agent_versions
         WHERE agent_id = ? AND tenant_id = ? AND version_no = ?
        """,
        (agent_id, tenant_id, version_no),
    ).fetchone()
    if row is None:
        raise AdoptionError("ADOPTION_SNAPSHOT_MISSING")
    try:
        parsed = json.loads(row["snapshot_json"])
    except ValueError as exc:
        raise AdoptionError("ADOPTION_SNAPSHOT_INVALID") from exc
    if not isinstance(parsed, dict):
        raise AdoptionError("ADOPTION_SNAPSHOT_INVALID")
    return parsed


def _verify_active_bindings_precommit(
    conn,
    *,
    planned_active_rows: list[dict[str, Any]],
    excluded_environments: tuple[str, ...],
    consumer_snapshots: dict[str, dict[str, Any]] | None = None,
    bindings_file: Path | None = None,
) -> None:
    """在事务提交与备份落盘前，按真实 ``_bindings_from_store`` 语义校验全部预期活跃路由。

    涵盖本次激活的目标路由以及库内其余未触碰环境的活跃路由，确保配套
    ``agent-runs.json``、``workflow_key_ref``、快照与可选静态路由文件无重名冲突全部通过；
    任一项失败即抛 ``ADOPTION_BINDING_VERIFICATION_FAILED`` 并整体回滚，不留半迁移态或孤儿备份。
    """
    overrides = consumer_snapshots or {}
    placeholders = ",".join("?" for _ in excluded_environments)
    untouched_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT environment, tenant_id, entry, scenario, engine, agent_id,
                   agent_version_no, app_id, workflow_id, workflow_key_ref,
                   revision, node_config_file
              FROM chat_route_bindings
             WHERE state = 'active' AND environment NOT IN ({placeholders})
             ORDER BY environment, tenant_id, entry, scenario
            """,
            excluded_environments,
        ).fetchall()
    ]

    all_rows = [*planned_active_rows, *untouched_rows]
    planned_keys: set[str] = set()
    for row in all_rows:
        env = str(row["environment"])
        tenant_id = str(row["tenant_id"])
        agent_id = str(row["agent_id"])
        version_no = int(row["agent_version_no"])
        scenario = str(row["scenario"])
        engine = str(row["engine"])

        binding = chat_execution.ChatBinding(
            environment=env,
            tenant_id=tenant_id,
            entry=str(row["entry"]),
            scenario=scenario,
            config_file="",
            manifest_file="",
            engine=engine,
            workflow_id=str(row["workflow_id"]),
            workflow_key_file=str(row["workflow_key_ref"]),
            agent_version_no=version_no,
            agent_id=agent_id,
            app_id=str(row["app_id"]),
            route_revision=int(row["revision"]),
        )
        if scenario == chat_execution.SCENARIO_CONSUMER_SERVICE:
            snapshot = (
                overrides[agent_id]
                if row in planned_active_rows and agent_id in overrides
                else _load_snapshot(conn, agent_id, tenant_id, version_no)
            )
            try:
                _validate_consumer_snapshot(
                    snapshot,
                    app_id=binding.app_id,
                    workflow_id=binding.workflow_id,
                    tenant_id=tenant_id,
                    environment=env,
                )
            except AdoptionError as exc:
                raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED") from exc
        if engine == chat_execution.ENGINE_NODE:
            try:
                snapshot = _load_snapshot(conn, agent_id, tenant_id, version_no)
                manifest_text = _validate_node_snapshot(snapshot)
            except AdoptionError as exc:
                raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED") from exc
            binding = chat_execution.ChatBinding(
                environment=binding.environment,
                tenant_id=binding.tenant_id,
                entry=binding.entry,
                scenario=binding.scenario,
                config_file=str(row["node_config_file"]),
                manifest_file="",
                manifest_text=manifest_text,
                engine=binding.engine,
                workflow_id=binding.workflow_id,
                workflow_key_file=binding.workflow_key_file,
                agent_version_no=binding.agent_version_no,
                agent_id=binding.agent_id,
                app_id=binding.app_id,
                route_revision=binding.route_revision,
            )
        try:
            chat_execution._validate_binding(binding)
        except chat_execution.ChatBindingError as exc:
            raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED") from exc
        planned_keys.add(binding.key)

    if bindings_file is not None:
        try:
            file_keys = {item.key for item in chat_execution.load_bindings(str(bindings_file))}
        except chat_execution.ChatBindingError as exc:
            raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED") from exc
        if file_keys & planned_keys:
            raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED")


def _public_summary(manifest: dict[str, Any], *, mode: str, changed: bool) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "mode": mode,
        "changed": changed,
        "source": manifest["source"],
        "target": manifest["target"],
        "tenants": manifest["tenants"],
        "routes": [
            {
                "tenant_id": r["tenant_id"],
                "entry": r["entry"],
                "scenario": r["scenario"],
                "engine": r["engine"],
                "agent_id": r["agent_id"],
                "app_id": r["app_id"],
                "from_version_no": r["agent_version_no"],
                "to_version_no": r["target_version_no"],
            }
            for r in manifest["routes"]
        ],
        "consumer_versions": manifest["agents"],
        "operator_apps_count": len(manifest["operator_apps"]),
        "orphan_operator_apps": [
            {"app_id": a["app_id"], "tenant_id": a["tenant_id"], "environment": a["environment"]}
            for a in manifest["orphan_operator_apps"]
        ],
        "grants": {
            "total": len(manifest["grants"]),
            "active": sum(1 for g in manifest["grants"] if g["revoked_at"] is None),
            "revoked": sum(1 for g in manifest["grants"] if g["revoked_at"] is not None),
        },
        "operator_conversations_count": len(manifest["operator_conversations"]),
        "skipped_operator_conversations": manifest["skipped_operator_conversations"],
        "config_refs": manifest["config_refs"],
    }


def _is_int(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _validate_backup_manifest(manifest: Any, *, db_path: Path, source: str, target: str) -> dict[str, Any]:
    """在触碰数据库前严格校验备份清单的结构、类型与交叉引用一致性。"""
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SCHEMA
        or manifest.get("database") != str(db_path)
        or (manifest.get("source"), manifest.get("target")) != (source, target)
    ):
        raise AdoptionError("ADOPTION_BACKUP_MISMATCH")

    required_keys = {
        "actor",
        "tenants",
        "routes",
        "agents",
        "operator_apps",
        "orphan_operator_apps",
        "grants",
        "operator_conversations",
        "skipped_operator_conversations",
        "config_refs",
    }
    if not required_keys.issubset(manifest):
        raise AdoptionError("ADOPTION_BACKUP_INVALID")
    if (
        not isinstance(manifest["actor"], str)
        or not isinstance(manifest["tenants"], list)
        or not all(isinstance(t, str) for t in manifest["tenants"])
        or not isinstance(manifest["routes"], list)
        or not manifest["routes"]
        or not isinstance(manifest["agents"], list)
        or not isinstance(manifest["operator_apps"], list)
        or not isinstance(manifest["orphan_operator_apps"], list)
        or not isinstance(manifest["grants"], list)
        or not isinstance(manifest["operator_conversations"], list)
        or not isinstance(manifest["skipped_operator_conversations"], dict)
        or not isinstance(manifest["config_refs"], list)
    ):
        raise AdoptionError("ADOPTION_BACKUP_INVALID")

    expected_consumer_agents: set[tuple[str, str, str, int, int]] = set()
    expected_op_bindings: dict[str, str] = {}
    for route in manifest["routes"]:
        if not isinstance(route, dict):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")
        str_fields = (
            "tenant_id",
            "entry",
            "scenario",
            "engine",
            "agent_id",
            "app_id",
            "workflow_id",
            "workflow_key_ref",
            "node_config_file",
        )
        if not all(isinstance(route.get(k), str) for k in str_fields) or not all(
            _is_int(route.get(k), minimum=1)
            for k in ("agent_version_no", "target_version_no", "revision", "target_revision")
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")
        if route["scenario"] == chat_execution.SCENARIO_CONSUMER_SERVICE:
            expected_consumer_agents.add(
                (
                    route["agent_id"],
                    route["tenant_id"],
                    route["app_id"],
                    route["agent_version_no"],
                    route["target_version_no"],
                )
            )
        if route["entry"] == "operator":
            to_key = f"{route['scenario']}:{target}:{route['tenant_id']}:{route['app_id']}:formal"
            from_key = f"{route['scenario']}:{source}:{route['tenant_id']}:{route['app_id']}:formal"
            expected_op_bindings[to_key] = from_key

    listed_consumer_agents: set[tuple[str, str, str, int, int]] = set()
    for ag in manifest["agents"]:
        if not isinstance(ag, dict):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")
        from_pub = ag.get("from_published_version", ag.get("from_version_no"))
        if (
            not all(isinstance(ag.get(k), str) for k in ("agent_id", "tenant_id", "app_id"))
            or not all(
                _is_int(ag.get(k), minimum=1)
                for k in ("from_version_no", "to_version_no", "from_revision", "to_revision")
            )
            or not _is_int(from_pub, minimum=1)
            or ag["to_revision"] != ag["from_revision"] + 2
            or ag["to_version_no"] <= ag["from_version_no"]
            or ag["to_version_no"] <= from_pub
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")
        listed_consumer_agents.add(
            (ag["agent_id"], ag["tenant_id"], ag["app_id"], ag["from_version_no"], ag["to_version_no"])
        )
    if (
        len(manifest["agents"]) != len(expected_consumer_agents)
        or listed_consumer_agents != expected_consumer_agents
    ):
        raise AdoptionError("ADOPTION_BACKUP_INVALID")

    for app in manifest["operator_apps"]:
        if (
            not isinstance(app, dict)
            or not isinstance(app.get("app_id"), str)
            or not _is_int(app.get("revision"), minimum=1)
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")

    for orphan in manifest["orphan_operator_apps"]:
        if not isinstance(orphan, dict) or not all(
            isinstance(orphan.get(k), str) for k in ("app_id", "tenant_id", "environment")
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")

    for grant in manifest["grants"]:
        if (
            not isinstance(grant, dict)
            or not all(
                isinstance(grant.get(k), str) for k in ("tenant_id", "knowledge_base_id", "granted_by")
            )
            or not _is_number(grant.get("granted_at"))
            or (grant.get("revoked_at") is not None and not _is_number(grant.get("revoked_at")))
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")

    for conv in manifest["operator_conversations"]:
        if (
            not isinstance(conv, dict)
            or not all(
                isinstance(conv.get(k), str) for k in ("conversation_id", "from_binding", "to_binding")
            )
            or not _is_int(conv.get("last_turn_no"), minimum=0)
            or conv["to_binding"] not in expected_op_bindings
            or conv["from_binding"] != expected_op_bindings[conv["to_binding"]]
        ):
            raise AdoptionError("ADOPTION_BACKUP_INVALID")

    return manifest


def _restore_from_backup(
    conn,
    *,
    db_path: Path,
    source: str,
    target: str,
    actor: str,
    backup_path: Path,
    verify_bindings: bool,
    bindings_file: Path | None = None,
) -> dict[str, Any]:
    try:
        raw_manifest = json.loads(backup_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdoptionError("ADOPTION_BACKUP_INVALID") from exc
    manifest = _validate_backup_manifest(raw_manifest, db_path=db_path, source=source, target=target)

    _assert_schema_present(conn)
    _assert_no_inflight_work(conn)

    # 1) 逐项 CAS 漂移核验：先全部只读比对，任何一项漂移即整体拒绝。
    current_target_routes = [
        dict(r)
        for r in conn.execute(
            """
            SELECT tenant_id, entry, scenario, engine, agent_id, agent_version_no,
                   app_id, workflow_id, state, revision
              FROM chat_route_bindings
             WHERE environment = ?
             ORDER BY tenant_id, entry, scenario
            """,
            (target,),
        ).fetchall()
    ]
    expected_target_routes = [
        {
            "tenant_id": r["tenant_id"],
            "entry": r["entry"],
            "scenario": r["scenario"],
            "engine": r["engine"],
            "agent_id": r["agent_id"],
            "agent_version_no": r["target_version_no"],
            "app_id": r["app_id"],
            "workflow_id": r["workflow_id"],
            "state": "active",
            "revision": r["target_revision"],
        }
        for r in manifest["routes"]
    ]
    if current_target_routes != expected_target_routes:
        raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    for r in manifest["routes"]:
        src_row = conn.execute(
            """
            SELECT state, revision, agent_version_no
              FROM chat_route_bindings
             WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
            """,
            (source, r["tenant_id"], r["entry"], r["scenario"]),
        ).fetchone()
        if (
            src_row is None
            or src_row["state"] != "retracted"
            or int(src_row["revision"]) != int(r["revision"]) + 1
            or int(src_row["agent_version_no"]) != int(r["agent_version_no"])
        ):
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    for ag in manifest["agents"]:
        row = conn.execute(
            "SELECT status, revision, published_version FROM agents WHERE agent_id = ? AND tenant_id = ?",
            (ag["agent_id"], ag["tenant_id"]),
        ).fetchone()
        if (
            row is None
            or row["status"] != "published"
            or int(row["revision"]) != int(ag["to_revision"])
            or int(row["published_version"] or 0) != int(ag["to_version_no"])
        ):
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")
        _load_snapshot(conn, str(ag["agent_id"]), str(ag["tenant_id"]), int(ag["from_version_no"]))
        from_pub = int(ag.get("from_published_version", ag["from_version_no"]))
        _load_snapshot(conn, str(ag["agent_id"]), str(ag["tenant_id"]), from_pub)

    current_target_apps = [
        (str(r["app_id"]), str(r["environment"]), int(r["revision"]))
        for r in conn.execute(
            "SELECT app_id, environment, revision FROM operator_apps WHERE environment = ? ORDER BY app_id",
            (target,),
        ).fetchall()
    ]
    expected_target_apps = [(str(a["app_id"]), target, int(a["revision"])) for a in manifest["operator_apps"]]
    if current_target_apps != expected_target_apps:
        raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    current_target_grants = [
        (
            str(g["tenant_id"]),
            str(g["knowledge_base_id"]),
            str(g["granted_by"]),
            float(g["granted_at"]),
            None if g["revoked_at"] is None else float(g["revoked_at"]),
        )
        for g in conn.execute(
            """
            SELECT tenant_id, knowledge_base_id, granted_by, granted_at, revoked_at
              FROM consumer_knowledge_grants
             WHERE environment = ?
             ORDER BY tenant_id, knowledge_base_id
            """,
            (target,),
        ).fetchall()
    ]
    expected_target_grants = [
        (
            str(g["tenant_id"]),
            str(g["knowledge_base_id"]),
            str(g["granted_by"]),
            float(g["granted_at"]),
            None if g["revoked_at"] is None else float(g["revoked_at"]),
        )
        for g in manifest["grants"]
    ]
    if current_target_grants != expected_target_grants:
        raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    target_op_prefixes = _operator_prefixes(manifest["routes"], target)
    target_conv_clause, target_conv_params = _prefix_clause("operator_binding", target_op_prefixes)
    current_target_convs = [
        (str(c["conversation_id"]), str(c["operator_binding"]), int(c["last_turn_no"]))
        for c in conn.execute(
            f"""
            SELECT conversation_id, operator_binding, last_turn_no
              FROM conversations
             WHERE business_entry = 'operator' AND {target_conv_clause}
             ORDER BY conversation_id
            """,
            target_conv_params,
        ).fetchall()
    ]
    expected_target_convs = [
        (str(c["conversation_id"]), str(c["to_binding"]), int(c["last_turn_no"]))
        for c in manifest["operator_conversations"]
    ]
    if current_target_convs != expected_target_convs:
        raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    if verify_bindings:
        restored_active_rows = [
            {
                "environment": source,
                "tenant_id": r["tenant_id"],
                "entry": r["entry"],
                "scenario": r["scenario"],
                "engine": r["engine"],
                "agent_id": r["agent_id"],
                "agent_version_no": r["agent_version_no"],
                "app_id": r["app_id"],
                "workflow_id": r["workflow_id"],
                "workflow_key_ref": r["workflow_key_ref"],
                "revision": int(r["revision"]) + 2,
                "node_config_file": r["node_config_file"],
            }
            for r in manifest["routes"]
        ]
        _verify_active_bindings_precommit(
            conn,
            planned_active_rows=restored_active_rows,
            excluded_environments=(source, target),
            bindings_file=bindings_file,
        )

    # 2) 全项无漂移且提交前校验通过：单调推进修订并逐项恢复，绝不删行、不倒写修订、不复活撤销。
    now_iso = format_stored_time(now_utc())
    now_epoch = time.time()

    for r in manifest["routes"]:
        cur = conn.execute(
            """
            UPDATE chat_route_bindings
               SET state = 'retracted', revision = revision + 1, updated_by = ?, updated_at = ?
             WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ? AND revision = ?
            """,
            (actor, now_iso, target, r["tenant_id"], r["entry"], r["scenario"], r["target_revision"]),
        )
        if cur.rowcount != 1:
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")
        cur_src = conn.execute(
            """
            UPDATE chat_route_bindings
               SET state = 'active', revision = revision + 1, updated_by = ?, updated_at = ?
             WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
               AND state = 'retracted' AND revision = ?
            """,
            (actor, now_iso, source, r["tenant_id"], r["entry"], r["scenario"], int(r["revision"]) + 1),
        )
        if cur_src.rowcount != 1:
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    for ag in manifest["agents"]:
        restore_pub = int(ag.get("from_published_version", ag["from_version_no"]))
        cur = conn.execute(
            """
            UPDATE agents
               SET published_version = ?, revision = revision + 1, updated_at = ?
             WHERE agent_id = ? AND tenant_id = ? AND status = 'published'
               AND published_version = ? AND revision = ?
            """,
            (
                restore_pub,
                now_iso,
                ag["agent_id"],
                ag["tenant_id"],
                ag["to_version_no"],
                ag["to_revision"],
            ),
        )
        if cur.rowcount != 1:
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    for a in manifest["operator_apps"]:
        cur = conn.execute(
            """
            UPDATE operator_apps
               SET environment = ?
             WHERE app_id = ? AND environment = ? AND revision = ?
            """,
            (source, a["app_id"], target, a["revision"]),
        )
        if cur.rowcount != 1:
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    for g in manifest["grants"]:
        conn.execute(
            """
            UPDATE consumer_knowledge_grants
               SET revoked_at = COALESCE(revoked_at, ?)
             WHERE tenant_id = ? AND environment = ? AND knowledge_base_id = ?
            """,
            (now_epoch, g["tenant_id"], target, g["knowledge_base_id"]),
        )

    for c in manifest["operator_conversations"]:
        cur = conn.execute(
            """
            UPDATE conversations
               SET operator_binding = ?
             WHERE conversation_id = ? AND operator_binding = ? AND last_turn_no = ?
            """,
            (c["from_binding"], c["conversation_id"], c["to_binding"], c["last_turn_no"]),
        )
        if cur.rowcount != 1:
            raise AdoptionError("ADOPTION_RESTORE_DRIFT")

    return _public_summary(manifest, mode="restored", changed=True)
