"""环境目录只读运行总览：汇总授权环境、租户资源/能力验证与节点状态，绝不触碰远端或泄密。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
from typing import Any

from aiops_diagnostics.dify.dify_workspaces import DIFY_PLATFORM_WORKSPACE_ID
from aiops_diagnostics.execution import node_capabilities
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import environment_tasks, gateway_db
from aiops_diagnostics.operator.operator_cpms import OBJECT_CONTRACT, TOOL_CONTRACT

OVERVIEW_SCHEMA = "aiops-environment-overview/1"
_CAPABILITY_KEYS = ("consumer", "order", "offline")
_VALID_TOOL_CONTRACTS = frozenset({TOOL_CONTRACT, OBJECT_CONTRACT})
_OPERATOR_SCENARIO = "operator_order_diagnosis"
_CONSUMER_SCENARIO = "consumer_service"


def _control_version() -> str:
    try:
        return package_version("aiops-diagnostics")
    except PackageNotFoundError:
        return ""


def _operator_tools_metadata() -> tuple[str, list[str]]:
    raw = files("aiops_diagnostics.operator").joinpath("operator_tools.json").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    parsed = json.loads(raw.decode("utf-8"))
    names = [
        str(item["name"])
        for item in parsed.get("tools", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
    ]
    return digest, names


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _safe_query(
    conn: sqlite3.Connection, table: str, sql: str, params: tuple[Any, ...] = ()
) -> list[sqlite3.Row]:
    if not _table_exists(conn, table):
        return []
    return list(conn.execute(sql, params).fetchall())


def _is_sqlite_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    return header == b"SQLite format 3\x00"


def _normalize_task(row: dict[str, Any], registry_path: str, reference: str) -> dict[str, Any] | None:
    """与 environment_tasks.inspect() 保持同一状态口径：含时间过期与输入/授权漂移复核。"""
    try:
        public = environment_tasks._public(row)
        raw_req = json.loads(row["request"])
    except (KeyError, ValueError, TypeError):
        return None
    if not isinstance(raw_req, dict):
        return None
    if public["state"] in {"observed", "blocked"} and public["action"] != "environment-publish":
        try:
            environment_tasks._current(registry_path, raw_req, reference, reconciling=True)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            public["state"] = "expired"
    return public


def _load_locked_drafts_and_tasks(
    registry_path: str,
    allowed: set[str],
    reference: str,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """在同一目录锁与单只读连接内加载全部获授权草稿与任务，杜绝跨锁分脑。"""
    draft_only_ids: list[str] = []
    draft_map: dict[str, dict[str, Any]] = {}
    task_map: dict[str, list[dict[str, Any]]] = {}
    db_path = drafts._database(registry_path)
    if not db_path.is_file():
        return draft_only_ids, draft_map, task_map
    with gateway_db.connection(db_path, readonly=True) as conn:
        for row in _safe_query(
            conn,
            "environment_drafts",
            "SELECT environment, revision, registry_revision, body "
            "FROM environment_drafts ORDER BY environment",
        ):
            env_id = str(row["environment"])
            if "*" not in allowed and env_id not in allowed:
                continue
            draft_only_ids.append(env_id)
            try:
                parsed = json.loads(row["body"])
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            draft_map[env_id] = {
                "revision": int(row["revision"]),
                "registry_revision": int(row["registry_revision"]),
                "draft": parsed,
            }
        for row in _safe_query(
            conn,
            "environment_tasks",
            "SELECT * FROM environment_tasks ORDER BY updated_at DESC",
        ):
            env_id = str(row["environment"])
            if "*" not in allowed and env_id not in allowed:
                continue
            public = _normalize_task(dict(row), registry_path, reference)
            if public is not None:
                task_map.setdefault(env_id, []).append(public)
    return draft_only_ids, draft_map, task_map


def _load_routes_from_db(
    routes_db: Path,
    control_exec_env: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    active_route_count = 0
    if not _is_sqlite_file(routes_db):
        return routes, active_route_count
    with gateway_db.connection(routes_db, readonly=True) as conn:
        for row in _safe_query(
            conn,
            "chat_route_bindings",
            """
            SELECT tenant_id, entry, scenario, app_id, agent_version_no
              FROM chat_route_bindings
             WHERE environment = ? AND state = 'active'
             ORDER BY updated_at ASC, revision ASC
            """,
            (control_exec_env,),
        ):
            active_route_count += 1
            tenant_id = str(row["tenant_id"])
            entry = str(row["entry"])
            scenario = str(row["scenario"])
            if entry == "operator" and scenario == _OPERATOR_SCENARIO:
                routes[(tenant_id, "operator")] = {
                    "app_id": str(row["app_id"] or ""),
                    "version_no": int(row["agent_version_no"]),
                    "state": "active",
                    "source": "runtime",
                }
            elif entry == "consumer" and scenario == _CONSUMER_SCENARIO:
                routes[(tenant_id, "consumer")] = {
                    "app_id": str(row["app_id"] or ""),
                    "version_no": int(row["agent_version_no"]),
                    "state": "active",
                    "source": "runtime",
                }
    return routes, active_route_count


def _load_runtime_tables(
    database_file: str | Path,
    route_bindings_file: str | Path,
    allowed_env_ids: set[str],
    control_env: str,
    control_exec_env: str,
) -> tuple[
    dict[tuple[str, str], dict[str, str]],
    dict[tuple[str, str], dict[str, Any]],
    dict[str, int],
    list[dict[str, Any]],
    int,
]:
    workspaces: dict[tuple[str, str], dict[str, str]] = {}
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    grants: dict[str, int] = {}
    node_rows: list[dict[str, Any]] = []
    active_route_count = 0

    if database_file:
        db_path = Path(database_file)
        if _is_sqlite_file(db_path):
            with gateway_db.connection(db_path, readonly=True) as conn:
                for row in _safe_query(
                    conn,
                    "dify_workspaces",
                    "SELECT environment, tenant_id, workspace_id, setup_state "
                    "FROM dify_workspaces WHERE tenant_id != ?",
                    (DIFY_PLATFORM_WORKSPACE_ID,),
                ):
                    env_id = str(row["environment"])
                    if env_id not in allowed_env_ids:
                        continue
                    workspaces[(env_id, str(row["tenant_id"]))] = {
                        "id": str(row["workspace_id"] or ""),
                        "state": str(row["setup_state"] or "pending"),
                        "source": "runtime",
                    }
                if control_env in allowed_env_ids:
                    for row in _safe_query(
                        conn,
                        "consumer_knowledge_grants",
                        """
                        SELECT tenant_id, COUNT(*) AS cnt
                          FROM consumer_knowledge_grants
                         WHERE environment = ? AND revoked_at IS NULL
                         GROUP BY tenant_id
                        """,
                        (control_exec_env,),
                    ):
                        grants[str(row["tenant_id"])] = int(row["cnt"])
                    for row in _safe_query(
                        conn,
                        "execution_node_capabilities",
                        """
                        SELECT node_id, body, observed_at
                          FROM execution_node_capabilities
                         WHERE environment = ?
                         ORDER BY node_id
                        """,
                        (control_exec_env,),
                    ):
                        try:
                            observed_at = float(row["observed_at"])
                            if not math.isfinite(observed_at):
                                continue
                            parsed = json.loads(row["body"])
                        except (ValueError, TypeError):
                            parsed = {}
                        node_rows.append(
                            {
                                "node_id": str(row["node_id"]),
                                "observed_at": observed_at,
                                "body": parsed if isinstance(parsed, dict) else {},
                            }
                        )

    if control_env in allowed_env_ids:
        routes_target = Path(route_bindings_file) if route_bindings_file else Path(database_file or "")
        if str(routes_target):
            routes, active_route_count = _load_routes_from_db(routes_target, control_exec_env)

    return workspaces, routes, grants, node_rows, active_route_count


def _task_summary(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if task is None:
        return None
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    updated_at = task.get("updated_at")
    ts = float(updated_at) if isinstance(updated_at, (int, float)) and math.isfinite(updated_at) else 0.0
    return {
        "id": str(task["id"]),
        "action": str(task.get("action") or "site-preflight"),
        "tenant_id": str(task.get("tenant_id") or ""),
        "state": str(task["state"]),
        "phase": str(result.get("phase") or ""),
        "error_code": str(task.get("error_code") or ""),
        "updated_at": ts,
    }


def _find_latest(
    tasks: list[dict[str, Any]],
    actions: set[str],
    *,
    tenant_id: str | None = None,
    states: set[str] | None = None,
    result_field: str | None = None,
    confirmed_only: bool = False,
) -> dict[str, Any] | None:
    for item in tasks:
        if item.get("action") not in actions:
            continue
        if tenant_id is not None and item.get("tenant_id") != tenant_id:
            continue
        if states is not None and item.get("state") not in states:
            continue
        res = item.get("result")
        if confirmed_only:
            # 仅采信已确认落成的回执（排除 blocked/cancelled/queued/running/reconcile 及预分配幽灵编号）。
            if item.get("state") not in {"observed", "expired"}:
                continue
            if not isinstance(res, dict) or res.get("state") not in {None, "observed"}:
                continue
        if result_field is not None and (not isinstance(res, dict) or not res.get(result_field)):
            continue
        return item
    return None


def _build_control_nodes(
    node_rows: list[dict[str, Any]],
    now: float,
    tools_digest: str,
    tool_names: list[str],
    gateway_version: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for row in node_rows:
        observed_at = row["observed_at"]
        age = max(0, int(now - observed_at))
        online = (now - observed_at) <= node_capabilities.MAX_AGE
        body = row["body"]
        raw_schemas = body.get("schemas")
        schemas = [str(s) for s in raw_schemas if isinstance(s, str)] if isinstance(raw_schemas, list) else []
        tool_contract = str(body.get("tool_contract") or "")
        compatible = bool(
            body.get("tools_sha256")
            and body.get("tools_sha256") == tools_digest
            and tool_contract in _VALID_TOOL_CONTRACTS
        )
        nodes.append(
            {
                "source": "heartbeat",
                "node_id": row["node_id"],
                "status": "online" if online else "stale",
                "observed_at": observed_at,
                "age_seconds": age,
                "version": gateway_version,
                "tool_contract": tool_contract,
                "schemas": schemas,
                "tools_compatible": compatible,
                "available_tools": list(tool_names) if compatible else [],
            }
        )
    return nodes


def _build_remote_node_receipts(env_id: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总远端受管任务点时回执；严格隔离主机验证、安装与预检，绝不把过期/受阻升级为已观察。"""
    host_task = _find_latest(tasks, {"host-verify"})
    install_task = _find_latest(tasks, {"candidate-install", "site-install"})
    preflight_task = _find_latest(tasks, {"site-preflight"})
    observing = [t for t in (host_task, install_task, preflight_task) if t is not None]
    if not observing:
        return []

    evidence: dict[str, Any] = {}
    if host_task is not None and isinstance(host_task.get("result"), dict):
        raw_ev = host_task["result"].get("evidence")
        if isinstance(raw_ev, dict):
            evidence = raw_ev

    remote_hostname = str(evidence.get("remote_hostname") or "")
    service_user_present = (
        bool(evidence["service_user_present"]) if "service_user_present" in evidence else None
    )

    version = ""
    for candidate in (install_task, preflight_task):
        if candidate is None or not isinstance(candidate.get("result"), dict):
            continue
        raw_ver = candidate["result"].get("version")
        if isinstance(raw_ver, str) and raw_ver:
            version = raw_ver
            break

    # 安装状态仅从安装类任务确认回执提取，避免被后续 site-preflight 或排队 stub 覆盖。
    installation = ""
    confirmed_install = _find_latest(
        tasks,
        {"candidate-install", "site-install"},
        confirmed_only=True,
        result_field="installation",
    )
    if confirmed_install is not None:
        raw_inst = confirmed_install["result"].get("installation")
        if isinstance(raw_inst, str) and raw_inst != "not_executed":
            installation = raw_inst
    elif install_task is not None and isinstance(install_task.get("result"), dict):
        raw_inst = install_task["result"].get("installation")
        if isinstance(raw_inst, str):
            installation = raw_inst

    primary = max(observing, key=lambda t: float(t.get("updated_at") or 0.0))
    states = {str(t["state"]) for t in observing}
    if "blocked" in states:
        status = "blocked"
    elif "reconcile" in states:
        status = "reconcile"
    elif "observed" in states and not (states & {"expired"}):
        status = "observed"
    elif "expired" in states:
        status = "expired"
    else:
        status = "unobserved"

    updated = primary.get("updated_at")
    observed_at = float(updated) if isinstance(updated, (int, float)) and math.isfinite(updated) else 0.0
    return [
        {
            "source": "task_receipt",
            "node_id": remote_hostname or env_id,
            "status": status,
            "observed_at": observed_at,
            "version": version,
            "remote_hostname": remote_hostname,
            "service_user_present": service_user_present,
            "installation": installation,
            "task_id": str(primary["id"]),
            "task_action": str(primary.get("action") or ""),
        }
    ]


def _build_tenant_capabilities(
    env_tasks: list[dict[str, Any]], tenant_id: str, now: float
) -> dict[str, dict[str, Any]]:
    latest = _find_latest(env_tasks, {"candidate-validate"}, tenant_id=tenant_id)
    base = {
        cap: {"state": "unverified", "error_code": "", "observed_at": None, "task_id": ""}
        for cap in _CAPABILITY_KEYS
    }
    if latest is None:
        return base
    task_state = str(latest["state"])
    task_id = str(latest["id"])
    raw_updated = latest.get("updated_at")
    updated_at = (
        float(raw_updated) if isinstance(raw_updated, (int, float)) and math.isfinite(raw_updated) else 0.0
    )
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    raw_expires = result.get("expires_at")
    result_expired = not (
        isinstance(raw_expires, (int, float)) and math.isfinite(raw_expires) and raw_expires > now
    )
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    by_cap = {
        str(c.get("capability")): c
        for c in checks
        if isinstance(c, dict) and str(c.get("capability") or "") in _CAPABILITY_KEYS
    }
    for cap in _CAPABILITY_KEYS:
        entry = by_cap.get(cap)
        if entry is None:
            continue
        if task_state == "expired" or result_expired:
            mapped = "expired"
        elif task_state == "cancelled":
            mapped = "unverified"
        else:
            raw = str(entry.get("state") or "pending")
            mapped = raw if raw in {"passed", "blocked", "pending"} else "pending"
        base[cap] = {
            "state": mapped,
            "error_code": str(entry.get("error_code") or ""),
            "observed_at": updated_at,
            "task_id": task_id,
        }
    return base


def _build_tenant_overview(
    env_id: str,
    tenant_id: str,
    tenant_name: str,
    effective_tenant: dict[str, Any] | None,
    draft_tenant: dict[str, Any] | None,
    *,
    is_control: bool,
    env_enabled: bool,
    now: float,
    workspaces: dict[tuple[str, str], dict[str, str]],
    control_routes: dict[tuple[str, str], dict[str, Any]],
    control_grants: dict[str, int],
    env_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    if effective_tenant is not None:
        tenant_flag = bool(effective_tenant.get("enabled"))
        effective_enabled = bool(env_enabled and tenant_flag)
        admission = "enabled" if effective_enabled else "disabled"
    else:
        admission = "draft_only"
        effective_enabled = False
    draft_enabled = (
        bool(draft_tenant.get("enabled"))
        if isinstance(draft_tenant, dict) and "enabled" in draft_tenant
        else None
    )

    ws = workspaces.get((env_id, tenant_id))
    if ws is None:
        ws_task = _find_latest(
            env_tasks,
            {"workspace-prepare"},
            tenant_id=tenant_id,
            result_field="workspace_id",
            confirmed_only=True,
        )
        ws_id = str(ws_task["result"]["workspace_id"]) if ws_task else ""
        ws = (
            {"id": ws_id, "state": "created", "source": "task_receipt"}
            if ws_id
            else {"id": "", "state": "unprepared", "source": "none"}
        )

    kb_task = _find_latest(
        env_tasks,
        {"knowledge-prepare"},
        tenant_id=tenant_id,
        result_field="knowledge_base_id",
        confirmed_only=True,
    )
    conn_task = _find_latest(
        env_tasks,
        {"knowledge-connect"},
        tenant_id=tenant_id,
        result_field="connection_id",
        confirmed_only=True,
    )
    kb_id = str(kb_task["result"]["knowledge_base_id"]) if kb_task else ""
    conn_id = str(conn_task["result"]["connection_id"]) if conn_task else ""
    active_grants = control_grants.get(tenant_id, 0) if is_control else 0
    kb_state = (
        "ready" if (active_grants > 0 or (kb_id and conn_id)) else "prepared" if kb_id else "unprepared"
    )

    def _app_entry(entry_key: str, task_action: str) -> dict[str, Any]:
        if is_control:
            # 控制环境以实时运行路由表为唯一活跃真相；撤回后绝不回退任务回执伪报运行中。
            if (tenant_id, entry_key) in control_routes:
                return dict(control_routes[(tenant_id, entry_key)])
        else:
            pub_task = _find_latest(env_tasks, {"environment-publish"}, tenant_id=tenant_id)
            if (
                env_enabled
                and effective_enabled
                and pub_task
                and pub_task["state"] == "observed"
                and isinstance(pub_task.get("result"), dict)
            ):
                pub_res = pub_task["result"]
                if (
                    pub_res.get("activated") is True
                    and pub_res.get("phase") == "activated"
                    and pub_res.get("directory") == "synchronized"
                ):
                    pub_versions = (
                        pub_res.get("versions") if isinstance(pub_res.get("versions"), dict) else {}
                    )
                    item = pub_versions.get(entry_key)
                    if isinstance(item, dict) and item.get("app_id"):
                        ver = item.get("version_no")
                        return {
                            "app_id": str(item["app_id"]),
                            "version_no": int(ver) if type(ver) is int else None,
                            "state": "active",
                            "source": "task_receipt",
                        }
        prep_task = _find_latest(
            env_tasks,
            {task_action},
            tenant_id=tenant_id,
            result_field="app_id",
            confirmed_only=True,
        )
        if prep_task:
            return {
                "app_id": str(prep_task["result"]["app_id"]),
                "version_no": None,
                "state": "candidate",
                "source": "task_receipt",
            }
        return {"app_id": "", "version_no": None, "state": "unprepared", "source": "none"}

    tenant_tasks = [t for t in env_tasks if t.get("tenant_id") == tenant_id]
    return {
        "id": tenant_id,
        "name": tenant_name,
        "admission": admission,
        "effective_enabled": effective_enabled,
        "draft_enabled": draft_enabled,
        "workspace": ws,
        "knowledge": {
            "knowledge_base_id": kb_id,
            "connection_id": conn_id,
            "active_grants": active_grants,
            "state": kb_state,
        },
        "apps": {
            "operator": _app_entry("operator", "operator-prepare"),
            "consumer": _app_entry("consumer", "consumer-prepare"),
        },
        "capabilities": _build_tenant_capabilities(env_tasks, tenant_id, now),
        "accuracy_confirmed": False,
        "latest_task": _task_summary(tenant_tasks[0] if tenant_tasks else None),
    }


def _assemble_overview(
    context: Any,
    body: dict[str, Any],
    allowed: set[str],
    draft_only_ids: list[str],
    draft_map: dict[str, dict[str, Any]],
    task_map: dict[str, list[dict[str, Any]]],
    *,
    tolerate_runtime_errors: bool = False,
) -> dict[str, Any]:
    settings = context.settings
    control_env = str(getattr(settings, "environment", "") or "")
    control_exec_env = str(getattr(settings, "execution_environment", "") or control_env)
    database_file = getattr(settings, "database_file", "")
    route_bindings_file = getattr(settings, "chat_route_bindings_file", "")

    effective_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for row in body["environments"]:
        env_id = str(row["id"])
        if "*" in allowed or env_id in allowed:
            effective_by_id[env_id] = row
            ordered_ids.append(env_id)

    candidate_ids = set(ordered_ids)
    for env_id in draft_only_ids:
        if env_id not in candidate_ids:
            candidate_ids.add(env_id)
            ordered_ids.append(env_id)

    try:
        workspaces, control_routes, control_grants, node_rows, active_route_count = _load_runtime_tables(
            database_file, route_bindings_file, candidate_ids, control_env, control_exec_env
        )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        if not tolerate_runtime_errors:
            raise
        workspaces, control_routes, control_grants, node_rows, active_route_count = {}, {}, {}, [], 0

    tools_digest, tool_names = _operator_tools_metadata()
    gateway_version = _control_version()
    now = time.time()
    control_visible = bool(control_env and ("*" in allowed or control_env in candidate_ids))

    environments: list[dict[str, Any]] = []
    for env_id in ordered_ids:
        eff = effective_by_id.get(env_id)
        saved = draft_map.get(env_id)
        draft_body = saved["draft"] if saved and isinstance(saved.get("draft"), dict) else None
        is_control = bool(control_env and env_id == control_env)
        registry_changed = bool(saved and saved["registry_revision"] != body["revision"])
        if eff is not None:
            status = "active" if eff.get("enabled") else "disabled"
            enabled = bool(eff.get("enabled"))
            display_name = str(eff.get("name") or env_id)
        else:
            status = "draft_only"
            enabled = False
            display_name = str((draft_body or {}).get("name") or env_id)

        raw_eff_tenants = eff.get("tenants") if isinstance(eff, dict) else []
        raw_draft_tenants = (draft_body or {}).get("tenants")
        eff_list = (
            [t for t in raw_eff_tenants if isinstance(t, dict) and t.get("id")]
            if isinstance(raw_eff_tenants, list)
            else []
        )
        draft_list = (
            [t for t in raw_draft_tenants if isinstance(t, dict) and t.get("id")]
            if isinstance(raw_draft_tenants, list)
            else []
        )
        eff_tenants = {str(t["id"]): t for t in eff_list}
        draft_tenants = {str(t["id"]): t for t in draft_list}
        tenant_ids: list[str] = []
        for source_list in (eff_list, draft_list):
            for item in source_list:
                tid = str(item["id"])
                if tid not in tenant_ids:
                    tenant_ids.append(tid)

        env_tasks = task_map.get(env_id, [])
        tenants = [
            _build_tenant_overview(
                env_id,
                tid,
                str((eff_tenants.get(tid) or draft_tenants.get(tid) or {}).get("name") or tid),
                eff_tenants.get(tid),
                draft_tenants.get(tid),
                is_control=is_control,
                env_enabled=enabled,
                now=now,
                workspaces=workspaces,
                control_routes=control_routes,
                control_grants=control_grants,
                env_tasks=env_tasks,
            )
            for tid in tenant_ids
        ]

        nodes = (
            _build_control_nodes(node_rows, now, tools_digest, tool_names, gateway_version)
            if is_control
            else _build_remote_node_receipts(env_id, env_tasks)
        )
        environments.append(
            {
                "id": env_id,
                "name": display_name,
                "is_control": is_control,
                "status": status,
                "enabled": enabled,
                "draft_revision": saved["revision"] if saved else None,
                "registry_changed": registry_changed,
                "tenant_count": len(tenants),
                "enabled_tenant_count": sum(1 for t in tenants if t["effective_enabled"]),
                "active_route_count": active_route_count if is_control else 0,
                "nodes": nodes,
                "tenants": tenants,
                "latest_task": _task_summary(env_tasks[0] if env_tasks else None),
            }
        )

    return {
        "schema": OVERVIEW_SCHEMA,
        "control_environment": control_env if control_visible else "",
        "control_execution_environment": control_exec_env if control_visible else "",
        "control_version": gateway_version if control_visible else "",
        "generated_at": now,
        "registry_revision": int(body["revision"]),
        "environments": environments,
    }


def build_overview_locked(context: Any, registry_path: str, identity: tuple[str, str, str]) -> dict[str, Any]:
    """在同一目录锁下重新校验授权并读取一致的目录/草稿/任务快照生成只读总览。"""
    reference = getattr(context.settings, "environment_onboarding_file", "")
    with drafts._locked(registry_path):
        body = registry.load(registry_path)
        if body["schema"] != registry.MANAGED_SCHEMA:
            raise registry.RegistryError("ENVIRONMENT_REGISTRY_MIGRATION_REQUIRED")
        allowed = drafts.access(body, identity)
        draft_only_ids, draft_map, task_map = _load_locked_drafts_and_tasks(registry_path, allowed, reference)
    return _assemble_overview(context, body, allowed, draft_only_ids, draft_map, task_map)


def catalogue_with_overview(
    context: Any, registry_path: str, identity: tuple[str, str, str]
) -> dict[str, Any]:
    """在同一目录锁内原子产出 drafts 目录与同快照总览，并隔离运行库瞬时故障不影响草稿编辑。"""
    reference = getattr(context.settings, "environment_onboarding_file", "")
    with drafts._locked(registry_path):
        body = registry.load(registry_path)
        if body["schema"] != registry.MANAGED_SCHEMA:
            raise registry.RegistryError("ENVIRONMENT_REGISTRY_MIGRATION_REQUIRED")
        allowed = drafts.access(body, identity)
        raw_rows = drafts._rows(drafts._database(registry_path))
        draft_only_ids, draft_map, task_map = _load_locked_drafts_and_tasks(registry_path, allowed, reference)
    catalogue_result = {
        "registry_revision": body["revision"],
        "can_create": "*" in allowed,
        "effective": [
            {k: v for k, v in r.items() if k != "config_ref"}
            for r in body["environments"]
            if "*" in allowed or r["id"] in allowed
        ],
        "drafts": [
            {
                **{k: v for k, v in r.items() if k != "body"},
                "draft": json.loads(r["body"]),
                "state": "draft",
                "acceptance": "unverified",
                "registry_changed": r["registry_revision"] != body["revision"],
            }
            for r in raw_rows
            if "*" in allowed or r["environment"] in allowed
        ],
    }
    try:
        catalogue_result["overview"] = _assemble_overview(
            context,
            body,
            allowed,
            draft_only_ids,
            draft_map,
            task_map,
            tolerate_runtime_errors=True,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, AttributeError):
        catalogue_result["overview"] = None
    return catalogue_result
