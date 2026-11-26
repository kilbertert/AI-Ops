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
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.dify.dify_chat_workflow import WORKFLOW_ID_PATTERN
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _PREVIEW_PREFIX as _PREVIEW_PREFIX,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _REQUIRED_TABLES as _REQUIRED_TABLES,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    SCHEMA as SCHEMA,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    AdoptionError as AdoptionError,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _assert_no_inflight_work as _assert_no_inflight_work,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _assert_schema_present as _assert_schema_present,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _is_int as _is_int,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _is_number as _is_number,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _load_snapshot as _load_snapshot,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _operator_prefixes as _operator_prefixes,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _prefix_clause as _prefix_clause,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _public_summary as _public_summary,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _restore_from_backup as _restore_from_backup,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _validate_backup_manifest as _validate_backup_manifest,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _validate_consumer_snapshot as _validate_consumer_snapshot,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _validate_node_snapshot as _validate_node_snapshot,
)
from aiops_diagnostics.onboarding.execution_adoption_restore import (
    _verify_active_bindings_precommit as _verify_active_bindings_precommit,
)
from aiops_diagnostics.onboarding.registry import ACTOR_PATTERN


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
