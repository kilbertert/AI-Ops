"""一次性受控执行环境迁移工件：显式切换旧执行环境键至唯一运行目录环境。

默认只读预演（SQLite ``?mode=ro``），不产生写副作用；提供 ``backup`` 时在单次
``BEGIN IMMEDIATE`` 事务内完成全部预检（含可选 ``verify_bindings``）、状态迁移与
回读断言，再在提交前以 ``O_EXCL|O_NOFOLLOW``（``0600``）落盘冻结清单并 ``fsync``，
保证任何预检、校验或写入失败均不留下已修改数据库或孤儿备份文件：

1. 为 ``consumer_service`` 路由按既有 Agent 发布合同生成新不可变版本，只改写
   ``consumer_service.content.environment``，不修改历史版本快照；
2. 复制 ``chat_route_bindings`` 活跃路由至目标环境，并单调撤回旧环境行（保留供
   ``by_key`` 溯源，绝不删行）；
3. 显式转移 ``operator_apps`` 中属于源环境的登记行（空串孤儿行原样保留并单列记录）；
4. 复制 ``consumer_knowledge_grants``（原样保留 ``granted_at`` 与 ``revoked_at``，不复活撤销）；
5. 显式迁移现役管家应用正式会话的 ``conversations.operator_binding``（不触碰预览或历史退役应用）。

恢复模式按冻结清单逐项 CAS 校验：业务开放后若路由、Agent、应用、授权或会话已漂移，
明确拒绝覆盖，绝不整库回滚、不倒写修订、不删历史、不复活已撤销授权。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.dify.dify_chat_workflow import WORKFLOW_ID_PATTERN
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.gateway.environment_registry import ACTOR_PATTERN
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


def _private_path(raw: str | Path, *, exists: bool) -> Path:
    path = Path(raw).expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise AdoptionError("ADOPTION_PATH_UNSAFE")
    if exists:
        try:
            validate_private_file(path)
        except PrivatePathError as exc:
            raise AdoptionError("ADOPTION_PATH_UNSAFE") from exc
        if not path.parent.is_dir() or path.parent.stat().st_mode & 0o002:
            raise AdoptionError("ADOPTION_PATH_UNSAFE")
    else:
        if path.exists() or path.is_symlink():
            raise AdoptionError("ADOPTION_BACKUP_EXISTS")
        if not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
            raise AdoptionError("ADOPTION_BACKUP_DIR_UNSAFE")
    return path


def _validate_inputs(
    database: str | Path,
    source: str,
    target: str,
    actor: str,
    backup: str | Path | None,
    restore: bool,
) -> tuple[Path, str, str, Path | None]:
    db_path = _private_path(database, exists=True)
    try:
        src = canonical_environment(source)
        dst = canonical_environment(target)
    except ValueError as exc:
        raise AdoptionError("ADOPTION_ENVIRONMENT_INVALID") from exc
    if src == dst:
        raise AdoptionError("ADOPTION_ENVIRONMENT_SAME")
    if not isinstance(actor, str) or not ACTOR_PATTERN.fullmatch(actor):
        raise AdoptionError("ADOPTION_ACTOR_INVALID")
    if restore and backup is None:
        raise AdoptionError("ADOPTION_BACKUP_REQUIRED")
    backup_path = _private_path(backup, exists=restore) if backup is not None else None
    return db_path, src, dst, backup_path


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


def _inspect_config_files(
    config_files: tuple[str | Path, ...], source: str, target: str
) -> list[dict[str, Any]]:
    """只读统计受控配置文件中的环境字段引用数，不输出完整路径或凭据正文。"""
    summaries = []
    for raw in config_files:
        path = _private_path(raw, exists=True)
        if path.stat().st_size > 262144:
            raise AdoptionError("ADOPTION_CONFIG_TOO_LARGE")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdoptionError("ADOPTION_CONFIG_INVALID") from exc
        src_hits, dst_hits = _count_env_fields(body, source, target)
        label = f"{path.parent.name}/{path.name}" if path.parent.name else path.name
        summaries.append(
            {
                "name": label,
                "source_refs": src_hits,
                "target_refs": dst_hits,
            }
        )
    return summaries


def _count_env_fields(node: Any, source: str, target: str) -> tuple[int, int]:
    src_hits, dst_hits = 0, 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "environment" and isinstance(value, str):
                if value == source:
                    src_hits += 1
                elif value == target:
                    dst_hits += 1
            else:
                sub_s, sub_d = _count_env_fields(value, source, target)
                src_hits += sub_s
                dst_hits += sub_d
    elif isinstance(node, list):
        for item in node:
            sub_s, sub_d = _count_env_fields(item, source, target)
            src_hits += sub_s
            dst_hits += sub_d
    return src_hits, dst_hits


def _write_backup_exclusive(path: Path, payload: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


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


def _plan_migration(
    conn,
    *,
    db_path: Path,
    source: str,
    target: str,
    actor: str,
    config_refs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    _assert_schema_present(conn)
    _assert_no_inflight_work(conn)

    if conn.execute("SELECT 1 FROM chat_route_bindings WHERE environment = ? LIMIT 1", (target,)).fetchone():
        raise AdoptionError("ADOPTION_TARGET_ROUTE_CONFLICT")
    if conn.execute("SELECT 1 FROM operator_apps WHERE environment = ? LIMIT 1", (target,)).fetchone():
        raise AdoptionError("ADOPTION_TARGET_APP_CONFLICT")
    if conn.execute(
        "SELECT 1 FROM consumer_knowledge_grants WHERE environment = ? LIMIT 1", (target,)
    ).fetchone():
        raise AdoptionError("ADOPTION_TARGET_GRANT_CONFLICT")

    route_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT environment, tenant_id, entry, scenario, engine, agent_id,
                   agent_version_no, app_id, workflow_id, workflow_key_ref,
                   state, revision, updated_by, updated_at, node_config_file
              FROM chat_route_bindings
             WHERE environment = ? AND state = 'active'
             ORDER BY tenant_id, entry, scenario
            """,
            (source,),
        ).fetchall()
    ]
    if not route_rows:
        raise AdoptionError("ADOPTION_SOURCE_ROUTES_MISSING")

    target_op_prefixes = _operator_prefixes(route_rows, target)
    target_conv_clause, target_conv_params = _prefix_clause("operator_binding", target_op_prefixes)
    if conn.execute(
        f"SELECT 1 FROM conversations WHERE business_entry = 'operator' AND {target_conv_clause} LIMIT 1",
        target_conv_params,
    ).fetchone():
        raise AdoptionError("ADOPTION_TARGET_CONVERSATION_CONFLICT")

    consumer_plans: list[dict[str, Any]] = []
    consumer_snapshots: dict[str, dict[str, Any]] = {}
    seen_consumer_agents: set[str] = set()
    planned_routes: list[dict[str, Any]] = []

    for route in route_rows:
        snapshot = _load_snapshot(
            conn, str(route["agent_id"]), str(route["tenant_id"]), int(route["agent_version_no"])
        )

        target_version_no = int(route["agent_version_no"])
        if (
            route["engine"] == chat_execution.ENGINE_DIFY
            and WORKFLOW_ID_PATTERN.fullmatch(str(route["workflow_id"])) is None
        ):
            raise AdoptionError("ADOPTION_ROUTE_INVALID")

        if route["scenario"] == chat_execution.SCENARIO_CONSUMER_SERVICE:
            if route["agent_id"] in seen_consumer_agents:
                raise AdoptionError("ADOPTION_DUPLICATE_CONSUMER_AGENT")
            seen_consumer_agents.add(str(route["agent_id"]))

            agent_row = conn.execute(
                """
                SELECT agent_id, tenant_id, status, revision, published_version
                  FROM agents
                 WHERE agent_id = ? AND tenant_id = ?
                """,
                (route["agent_id"], route["tenant_id"]),
            ).fetchone()
            if (
                agent_row is None
                or agent_row["status"] != "published"
                or int(agent_row["published_version"] or 0) < 1
            ):
                raise AdoptionError("ADOPTION_AGENT_STATE_CONFLICT")
            _validate_consumer_snapshot(
                snapshot,
                app_id=str(route["app_id"]),
                workflow_id=str(route["workflow_id"]),
                tenant_id=str(route["tenant_id"]),
                environment=source,
            )
            max_ver = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS max_ver FROM agent_versions WHERE agent_id = ?",
                (route["agent_id"],),
            ).fetchone()
            target_version_no = int(max_ver["max_ver"]) + 1
            new_snapshot = copy.deepcopy(snapshot)
            new_snapshot["consumer_service"]["content"]["environment"] = target
            consumer_snapshots[str(route["agent_id"])] = new_snapshot
            consumer_plans.append(
                {
                    "agent_id": route["agent_id"],
                    "tenant_id": route["tenant_id"],
                    "app_id": route["app_id"],
                    "from_version_no": int(route["agent_version_no"]),
                    "from_published_version": int(agent_row["published_version"]),
                    "to_version_no": target_version_no,
                    "from_revision": int(agent_row["revision"]),
                    "to_revision": int(agent_row["revision"]) + 2,
                }
            )
        if route["engine"] == chat_execution.ENGINE_NODE:
            _validate_node_snapshot(snapshot)

        planned_routes.append(
            {
                **route,
                "target_environment": target,
                "target_version_no": target_version_no,
                "target_revision": 1,
            }
        )

    app_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT app_id, tenant_id, workspace_id, agent_name, profile_id,
                   revision, entry, scenario, environment
              FROM operator_apps
             WHERE environment = ?
             ORDER BY app_id
            """,
            (source,),
        ).fetchall()
    ]
    orphan_apps = [
        dict(row)
        for row in conn.execute(
            """
            SELECT app_id, tenant_id, entry, scenario, environment, profile_id
              FROM operator_apps
             WHERE environment = ''
             ORDER BY app_id
            """
        ).fetchall()
    ]
    grant_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT tenant_id, environment, knowledge_base_id, granted_by, granted_at, revoked_at
              FROM consumer_knowledge_grants
             WHERE environment = ?
             ORDER BY tenant_id, knowledge_base_id
            """,
            (source,),
        ).fetchall()
    ]

    active_operator_bindings = {
        f"{r['scenario']}:{source}:{r['tenant_id']}:{r['app_id']}:formal": (
            f"{r['scenario']}:{target}:{r['tenant_id']}:{r['app_id']}:formal"
        )
        for r in route_rows
        if r["entry"] == "operator"
    }
    conversations: list[dict[str, Any]] = []
    if active_operator_bindings:
        placeholders = ",".join("?" for _ in active_operator_bindings)
        for row in conn.execute(
            f"""
            SELECT conversation_id, operator_binding, last_turn_no
              FROM conversations
             WHERE business_entry = 'operator' AND operator_binding IN ({placeholders})
             ORDER BY conversation_id
            """,
            tuple(sorted(active_operator_bindings)),
        ).fetchall():
            old_key = str(row["operator_binding"])
            conversations.append(
                {
                    "conversation_id": str(row["conversation_id"]),
                    "from_binding": old_key,
                    "to_binding": active_operator_bindings[old_key],
                    "last_turn_no": int(row["last_turn_no"]),
                }
            )
    preview_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM conversations"
            " WHERE business_entry = 'operator' AND substr(operator_binding, 1, ?) = ?",
            (len(_PREVIEW_PREFIX), _PREVIEW_PREFIX),
        ).fetchone()[0]
    )
    source_op_prefixes = _operator_prefixes(route_rows, source)
    source_conv_clause, source_conv_params = _prefix_clause("operator_binding", source_op_prefixes)
    all_source_op_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM conversations WHERE business_entry = 'operator' AND {source_conv_clause}",
            source_conv_params,
        ).fetchone()[0]
    )
    skipped_conversations = {
        "preview": preview_count,
        "retired_or_other_app": max(0, all_source_op_count - len(conversations)),
    }

    tenants = sorted(
        {r["tenant_id"] for r in route_rows}
        | {r["tenant_id"] for r in app_rows}
        | {r["tenant_id"] for r in grant_rows}
    )

    manifest = {
        "schema": SCHEMA,
        "database": str(db_path),
        "source": source,
        "target": target,
        "actor": actor,
        "tenants": tenants,
        "routes": planned_routes,
        "agents": consumer_plans,
        "operator_apps": app_rows,
        "orphan_operator_apps": orphan_apps,
        "grants": grant_rows,
        "operator_conversations": conversations,
        "skipped_operator_conversations": skipped_conversations,
        "config_refs": config_refs,
    }
    return manifest, consumer_snapshots


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


def _planned_target_active_rows(manifest: dict[str, Any], target: str) -> list[dict[str, Any]]:
    return [
        {
            "environment": target,
            "tenant_id": r["tenant_id"],
            "entry": r["entry"],
            "scenario": r["scenario"],
            "engine": r["engine"],
            "agent_id": r["agent_id"],
            "agent_version_no": r["target_version_no"],
            "app_id": r["app_id"],
            "workflow_id": r["workflow_id"],
            "workflow_key_ref": r["workflow_key_ref"],
            "revision": r["target_revision"],
            "node_config_file": r["node_config_file"],
        }
        for r in manifest["routes"]
    ]


def _verify_store_postcommit(
    db_path: Path, expected_env: str, expected_count: int, *, bindings_file: Path | None = None
) -> None:
    try:
        loaded = chat_execution._bindings_from_store(str(db_path))
        if bindings_file is not None:
            file_keys = {item.key for item in chat_execution.load_bindings(str(bindings_file))}
            if file_keys & {item.key for item in loaded}:
                raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED")
    except chat_execution.ChatBindingError as exc:
        raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED") from exc
    if sum(1 for b in loaded if b.environment == expected_env) != expected_count:
        raise AdoptionError("ADOPTION_BINDING_VERIFICATION_FAILED")


def adopt(
    database: str | Path,
    source: str,
    target: str,
    *,
    actor: str = "aiops-admin",
    backup: str | Path | None = None,
    restore: bool = False,
    config_files: tuple[str | Path, ...] = (),
    verify_bindings: bool = False,
    bindings_file: str | Path | None = None,
) -> dict[str, Any]:
    """执行只读预演、单事务迁移或逐项 CAS 恢复。"""
    db_path, src, dst, backup_path = _validate_inputs(database, source, target, actor, backup, restore)
    bindings_path = _private_path(bindings_file, exists=True) if bindings_file is not None else None
    config_refs = _inspect_config_files(config_files, src, dst) if config_files else []
    applying = backup_path is not None

    if restore:
        assert backup_path is not None
        with gateway_db.connection(db_path, write=True, profile=gateway_db.LEASE) as conn:
            summary = _restore_from_backup(
                conn,
                db_path=db_path,
                source=src,
                target=dst,
                actor=actor,
                backup_path=backup_path,
                verify_bindings=verify_bindings,
                bindings_file=bindings_path,
            )
        if verify_bindings:
            _verify_store_postcommit(db_path, src, len(summary["routes"]), bindings_file=bindings_path)
        return summary

    if not applying:
        with gateway_db.connection(db_path, write=False, readonly=True) as conn:
            conn.execute("BEGIN")
            try:
                manifest, consumer_snapshots = _plan_migration(
                    conn, db_path=db_path, source=src, target=dst, actor=actor, config_refs=config_refs
                )
                if verify_bindings:
                    _verify_active_bindings_precommit(
                        conn,
                        planned_active_rows=_planned_target_active_rows(manifest, dst),
                        excluded_environments=(src, dst),
                        consumer_snapshots=consumer_snapshots,
                        bindings_file=bindings_path,
                    )
            finally:
                conn.rollback()
        return _public_summary(manifest, mode="dry_run", changed=False)

    assert backup_path is not None
    backup_written = False
    try:
        with gateway_db.connection(db_path, write=True, profile=gateway_db.LEASE) as conn:
            manifest, consumer_snapshots = _plan_migration(
                conn, db_path=db_path, source=src, target=dst, actor=actor, config_refs=config_refs
            )
            if verify_bindings:
                _verify_active_bindings_precommit(
                    conn,
                    planned_active_rows=_planned_target_active_rows(manifest, dst),
                    excluded_environments=(src, dst),
                    consumer_snapshots=consumer_snapshots,
                    bindings_file=bindings_path,
                )

            now_iso = format_stored_time(now_utc())
            for ag in manifest["agents"]:
                snapshot_json = json.dumps(
                    consumer_snapshots[ag["agent_id"]], ensure_ascii=False, sort_keys=True
                )
                cur = conn.execute(
                    """
                    UPDATE agents
                       SET status = 'draft', revision = revision + 1, updated_at = ?
                     WHERE agent_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                    """,
                    (now_iso, ag["agent_id"], ag["tenant_id"], ag["from_revision"]),
                )
                if cur.rowcount != 1:
                    raise AdoptionError("ADOPTION_AGENT_STATE_CONFLICT")
                conn.execute(
                    """
                    INSERT INTO agent_versions
                        (agent_id, tenant_id, version_no, snapshot_json, published_by, published_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ag["agent_id"], ag["tenant_id"], ag["to_version_no"], snapshot_json, actor, now_iso),
                )
                cur = conn.execute(
                    """
                    UPDATE agents
                       SET status = 'published', published_version = ?,
                           revision = revision + 1, updated_at = ?
                     WHERE agent_id = ? AND tenant_id = ? AND status = 'draft' AND revision = ?
                    """,
                    (
                        ag["to_version_no"],
                        now_iso,
                        ag["agent_id"],
                        ag["tenant_id"],
                        ag["from_revision"] + 1,
                    ),
                )
                if cur.rowcount != 1:
                    raise AdoptionError("ADOPTION_AGENT_STATE_CONFLICT")

            for r in manifest["routes"]:
                # 单调撤回旧执行环境行（保留整行供 by_key 溯源，不删行）：
                # _bindings_from_store 会校验全部 active 节点路由与 node_config_file.environment
                # 一致，切换 agent-runs.json 后若旧节点路由仍 active 会直接令启动与路由加载崩溃。
                cur_src = conn.execute(
                    """
                    UPDATE chat_route_bindings
                       SET state = 'retracted', revision = revision + 1, updated_by = ?, updated_at = ?
                     WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                       AND state = 'active' AND revision = ?
                    """,
                    (actor, now_iso, src, r["tenant_id"], r["entry"], r["scenario"], r["revision"]),
                )
                if cur_src.rowcount != 1:
                    raise AdoptionError("ADOPTION_ROUTE_STATE_CONFLICT")
                conn.execute(
                    """
                    INSERT INTO chat_route_bindings
                        (environment, tenant_id, entry, scenario, engine, agent_id,
                         agent_version_no, app_id, workflow_id, workflow_key_ref,
                         state, revision, updated_by, updated_at, node_config_file)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (
                        dst,
                        r["tenant_id"],
                        r["entry"],
                        r["scenario"],
                        r["engine"],
                        r["agent_id"],
                        r["target_version_no"],
                        r["app_id"],
                        r["workflow_id"],
                        r["workflow_key_ref"],
                        actor,
                        now_iso,
                        r["node_config_file"],
                    ),
                )

            for a in manifest["operator_apps"]:
                cur = conn.execute(
                    """
                    UPDATE operator_apps
                       SET environment = ?
                     WHERE app_id = ? AND environment = ? AND revision = ?
                    """,
                    (dst, a["app_id"], src, a["revision"]),
                )
                if cur.rowcount != 1:
                    raise AdoptionError("ADOPTION_APP_STATE_CONFLICT")

            for g in manifest["grants"]:
                conn.execute(
                    """
                    INSERT INTO consumer_knowledge_grants
                        (tenant_id, environment, knowledge_base_id, granted_by, granted_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g["tenant_id"],
                        dst,
                        g["knowledge_base_id"],
                        g["granted_by"],
                        g["granted_at"],
                        g["revoked_at"],
                    ),
                )

            for c in manifest["operator_conversations"]:
                cur = conn.execute(
                    """
                    UPDATE conversations
                       SET operator_binding = ?
                     WHERE conversation_id = ? AND operator_binding = ? AND last_turn_no = ?
                    """,
                    (c["to_binding"], c["conversation_id"], c["from_binding"], c["last_turn_no"]),
                )
                if cur.rowcount != 1:
                    raise AdoptionError("ADOPTION_CONVERSATION_CONFLICT")

            # 事务提交前回读断言计数一致。
            migrated_routes = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chat_route_bindings WHERE environment = ? AND state = 'active'",
                    (dst,),
                ).fetchone()[0]
            )
            migrated_apps = int(
                conn.execute("SELECT COUNT(*) FROM operator_apps WHERE environment = ?", (dst,)).fetchone()[0]
            )
            migrated_grants = int(
                conn.execute(
                    "SELECT COUNT(*) FROM consumer_knowledge_grants WHERE environment = ?", (dst,)
                ).fetchone()[0]
            )
            if (
                migrated_routes != len(manifest["routes"])
                or migrated_apps != len(manifest["operator_apps"])
                or migrated_grants != len(manifest["grants"])
            ):
                raise AdoptionError("ADOPTION_READBACK_MISMATCH")

            # 全部预检、SQL 写入与回读断言在写锁内通过后，于 COMMIT 前落盘独占备份：
            # 前置失败绝不产生孤儿备份；写备份失败则事务自动回滚，绝不无备份提交。
            _write_backup_exclusive(backup_path, manifest)
            backup_written = True
    except Exception:
        if backup_written:
            with contextlib.suppress(OSError):
                backup_path.unlink()
        raise

    if verify_bindings:
        _verify_store_postcommit(db_path, dst, len(manifest["routes"]), bindings_file=bindings_path)

    return _public_summary(manifest, mode="migrated", changed=True)
