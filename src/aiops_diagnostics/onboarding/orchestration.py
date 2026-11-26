"""一键租户开通编排：沿既有受控原子阶段自动串联准备、安装、登记与可选验证；绝不自动启用正式准入。"""

from __future__ import annotations

import json
import time
from typing import Any

from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import candidate_preparation as preparation
from aiops_diagnostics.onboarding import context as management
from aiops_diagnostics.onboarding import (
    drafts,
    installation,
    plans,
    registration,
    registry,
    tasks,
    validation,
)
from aiops_diagnostics.onboarding.business import CAPABILITIES

ACTION = "tenant-onboard"
BASE_STAGES = ("candidate-prepare", "candidate-install", "candidate-register")
ALL_STAGES = (*BASE_STAGES, "candidate-validate")
# 阶段级续签余量：安装含 staging + 400s 传输上限 + 60s 授权保留 + 认领开销，必须保证充裕窗口。
_STAGE_MIN_REMAINING: dict[str, float] = {
    "candidate-install": 720.0,
    "candidate-register": 600.0,
    "candidate-validate": 600.0,
}
_INSTALL_PRE_DISPATCH_FLOOR = 600.0
_FRESH_SITE_MIN_REMAINING = 899.0


def _stage_sequence(request: dict[str, Any]) -> tuple[str, ...]:
    return ALL_STAGES if request.get("case_ref") else BASE_STAGES


def _renew_same_digest_plan(
    path: str,
    identity: tuple[str, str, str],
    original_plan: dict[str, Any],
    reference: str,
    *,
    min_remaining_seconds: float = 600.0,
    force_new: bool = False,
) -> dict[str, Any]:
    """仅当草稿、目录、制品引用与授权完全未漂移时续签同摘要计划窗，防止慢链与大包上传误超时。"""
    tasks._authorize(path, identity, original_plan["environment"])
    fresh = plans.preflight(
        path,
        identity,
        original_plan["environment"],
        original_plan["draft_revision"],
        original_plan["registry_revision"],
        reference,
        min_remaining_seconds=min_remaining_seconds,
        force_new=force_new,
    )
    if fresh["digest"] != original_plan["digest"] or fresh["state"] == "expired":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    if any(
        c["state"] != "passed"
        for c in fresh["checks"]
        if c["id"] not in {"host-identity", "capacity", "network", "business"}
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_BLOCKED")
    return fresh


def _site_baseline_status(
    conn, environment: str, site_row: dict[str, Any], *, exclude_id: str | None = None
) -> str:
    """检查目标主机在 site_row 之后是否有新的安装变更（含在飞安装）。"""
    rows = conn.execute(
        "SELECT id, state, updated_at, request FROM environment_tasks WHERE environment=? "
        "AND json_extract(request,'$.action') IN ('site-install','candidate-install')",
        (environment,),
    ).fetchall()
    site_time = float(site_row["updated_at"])
    for item in rows:
        if exclude_id and item["id"] == exclude_id:
            continue
        req = json.loads(item["request"])
        if item["state"] in ("executing", "checking"):
            return "busy"
        if (item["state"] == "observed" or req.get("dispatched")) and float(item["updated_at"]) >= site_time:
            return "stale"
    return "fresh"


def _validate_validation_inputs(payload: dict[str, Any]) -> tuple[str, list[str]]:
    has_case = "case_ref" in payload
    has_caps = "capabilities" in payload
    if not has_case and not has_caps:
        return "", []
    if not (has_case and has_caps):
        raise tasks.TaskRefused("ENVIRONMENT_VALIDATION_CASE_INVALID")
    case_ref = payload["case_ref"]
    caps = payload["capabilities"]
    if (
        not isinstance(case_ref, str)
        or not drafts.REFERENCE_PATTERN.fullmatch(case_ref)
        or not isinstance(caps, list)
        or not caps
        or any(not isinstance(c, str) for c in caps)
        or len(set(caps)) != len(caps)
        or not set(caps) <= set(CAPABILITIES)
    ):
        raise tasks.TaskRefused("ENVIRONMENT_VALIDATION_CASE_INVALID")
    return case_ref, sorted(caps)


def _stage_entry(action: str, row: dict[str, Any], public: dict[str, Any]) -> dict[str, Any]:
    result = public.get("result") if isinstance(public.get("result"), dict) else {}
    stage_state = "observed" if (row["state"] == "observed" and not row["cancel"]) else str(public["state"])
    entry: dict[str, Any] = {
        "action": action,
        "id": str(row["id"]),
        "state": stage_state,
        "error_code": str(public.get("error_code") or ""),
    }
    if isinstance(result.get("version"), str) and result["version"]:
        entry["version"] = result["version"]
    if isinstance(result.get("installation"), str) and result["installation"]:
        entry["installation"] = result["installation"]
    if isinstance(result.get("checks"), list):
        entry["checks"] = result["checks"]
    return entry


def submit(context: Any, identity: tuple[str, str, str], payload: dict[str, Any], source: dict[str, Any]):
    settings = context.settings
    path = management._path(context)
    reference = settings.environment_onboarding_file
    tenant_id = payload["tenant_id"]
    if not isinstance(tenant_id, str) or not drafts.REFERENCE_PATTERN.fullmatch(tenant_id):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
    initial = payload.get("initial_installation") is True
    if "initial_installation" in payload and not initial:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
    case_ref, capabilities = _validate_validation_inputs(payload)

    # 先行校验安装目标配置就绪，避免在缺少安装引用时白跑完准备阶段才阻塞。
    if not getattr(settings, "environment_installation_file", ""):
        raise tasks.TaskRefused("ENVIRONMENT_INSTALLATION_CONFIG_REQUIRED")

    plan = plans.read(path, identity, payload["onboard_plan"], reference)
    if plan["state"] == "expired":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    environment = plan["environment"]
    with drafts._locked(path):
        tasks._authorize(path, identity, environment)
        proposed_row = next(
            (r for r in drafts._rows(drafts._database(path)) if r["environment"] == environment),
            None,
        )
        if proposed_row is None:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        proposed_body = json.loads(proposed_row["body"])
        if not any(t["id"] == tenant_id for t in proposed_body["tenants"]):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
        target_id = proposed_body["deployment_target"]
    _, node_binding = installation.binding(settings, target_id)

    bindings = preparation._bindings(settings, source)
    identifier = (
        "task_"
        + plans._digest(
            {
                "action": ACTION,
                "plan": plan["id"],
                "tenant": tenant_id,
                "source": source,
                "bindings": bindings,
                "node_binding": node_binding,
                **({"initial_installation": True} if initial else {}),
                **({"case_ref": case_ref, "capabilities": capabilities} if case_ref else {}),
            }
        )[:32]
    )

    # 先校验是否已存在相同幂等任务、并发在飞任务，以及同计划下是否已有过期主机基线（如前一租户已安装）。
    need_fresh_site = False
    with drafts._locked(path), gateway_db.initialize(drafts._database(path)) as conn:
        drafts.initialize_tasks(conn)
        existing = conn.execute("SELECT * FROM environment_tasks WHERE id=?", (identifier,)).fetchone()
        if existing is not None:
            return tasks._public(dict(existing))
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? "
            "AND state IN ('waiting','checking') AND id!=? "
            "AND json_extract(request,'$.action')=? AND json_extract(request,'$.tenant_id')=? LIMIT 1",
            (environment, identifier, ACTION, tenant_id),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        if not initial:
            existing_site = conn.execute(
                "SELECT * FROM environment_tasks WHERE environment=? "
                "AND COALESCE(json_extract(request,'$.action'),'site-preflight')='site-preflight' "
                "AND json_extract(request,'$.plan.id')=? ORDER BY updated_at DESC LIMIT 1",
                (environment, plan["id"]),
            ).fetchone()
            if existing_site is not None and (
                existing_site["cancel"]
                or existing_site["state"] == "cancelled"
                or _site_baseline_status(conn, environment, dict(existing_site)) != "fresh"
            ):
                need_fresh_site = True

    active_plan = (
        _renew_same_digest_plan(
            path,
            identity,
            plan,
            reference,
            min_remaining_seconds=_FRESH_SITE_MIN_REMAINING,
            force_new=True,
        )
        if need_fresh_site
        else plan
    )
    prep_payload: dict[str, Any] = {
        "candidate_plan": active_plan["id"],
        "tenant_id": tenant_id,
        "source_app_id": payload["source_app_id"],
        "source_version": payload["source_version"],
    }
    if initial:
        prep_payload["initial_installation"] = True
    request: dict[str, Any] = {
        "identity": list(identity),
        "action": ACTION,
        "tenant_id": tenant_id,
        "plan": active_plan,
        "target_id": target_id,
        "consumer_source": source,
        "bindings": bindings,
        "node_binding": node_binding,
        "attempted": True,
        "children": {},
        **({"initial_installation": True} if initial else {}),
        **({"case_ref": case_ref, "capabilities": capabilities} if case_ref else {}),
    }
    initial_result: dict[str, Any] = {
        "scope": "tenant-one-click-onboarding",
        "executable": False,
        "expires_at": active_plan["expires_at"],
        "stages": [],
        "ready_to_publish": False,
        "activation": "not_executed",
        "business_accuracy": "unconfirmed",
        "validation_task_id": "",
        "registration_task_id": "",
        "installation_task_id": "",
        "preparation_task_id": "",
    }
    return preparation.submit(
        context,
        identity,
        prep_payload,
        source,
        onboard_parent=identifier,
        onboard_record=(request, initial_result),
    )


def advance(context: Any) -> dict[str, int]:
    """沿既有 worker 唤醒推进全部等待或对账中的一键开通父任务，零新增常驻守护进程。"""
    path = management._path(context)
    reference = context.settings.environment_onboarding_file
    if not drafts._database(path).exists():
        return {"advanced": 0}
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
            return {"advanced": 0}
        identifiers = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM environment_tasks WHERE state IN ('waiting','checking') "
                "AND json_extract(request,'$.action')=? ORDER BY updated_at LIMIT 20",
                (ACTION,),
            )
        ]
    for identifier in identifiers:
        _advance_one(context, path, reference, identifier)
    return {"advanced": len(identifiers)}


def _record_parent(
    conn,
    identifier: str,
    request: dict[str, Any],
    stages: list[dict[str, Any]],
    *,
    state: str,
    error: str,
    expires_at: float,
    ready_to_publish: bool,
) -> None:
    children = request["children"]
    result = {
        "scope": "tenant-one-click-onboarding",
        "executable": False,
        "expires_at": expires_at,
        "stages": stages,
        "ready_to_publish": ready_to_publish,
        "activation": "not_executed",
        "business_accuracy": "unconfirmed",
        "preparation_task_id": children.get("candidate-prepare", ""),
        "installation_task_id": children.get("candidate-install", ""),
        "registration_task_id": children.get("candidate-register", ""),
        "validation_task_id": children.get("candidate-validate", ""),
    }
    conn.execute(
        "UPDATE environment_tasks SET request=?,state=?,result=?,error=?,updated_at=? WHERE id=?",
        (json.dumps(request), state, json.dumps(result), error, time.time(), identifier),
    )


def _collect_recorded_stages(conn: Any, request: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    stages: list[dict[str, Any]] = []
    last_expires = float(request["plan"]["expires_at"])
    for stage in _stage_sequence(request):
        child_id = request["children"].get(stage)
        if not child_id:
            break
        child_row = tasks._row(conn, child_id)
        child_pub = tasks._public(child_row)
        child_res = child_pub.get("result") if isinstance(child_pub.get("result"), dict) else {}
        if isinstance(child_res.get("expires_at"), (int, float)):
            last_expires = max(last_expires, float(child_res["expires_at"]))
        stages.append(_stage_entry(stage, child_row, child_pub))
    return stages, last_expires


def _record_cancelled_parent(conn: Any, identifier: str, request: dict[str, Any]) -> None:
    stages_cancel, last_cancel_exp = _collect_recorded_stages(conn, request)
    still_active = tasks._has_active_onboard_children(conn, identifier, request)
    _record_parent(
        conn,
        identifier,
        request,
        stages_cancel,
        state="checking" if still_active else "cancelled",
        error="" if still_active else "ENVIRONMENT_TASK_CANCELLED",
        expires_at=last_cancel_exp,
        ready_to_publish=False,
    )


def _inspect_stage_progression(conn: Any, request: dict[str, Any]) -> dict[str, Any]:
    sequence = _stage_sequence(request)
    stages: list[dict[str, Any]] = []
    state, error = "waiting", ""
    last_expires = float(request["plan"]["expires_at"])
    next_stage: str | None = None
    refresh_install_id: str | None = None

    for stage in sequence:
        child_id = request["children"].get(stage)
        if not child_id:
            next_stage = stage
            break
        child_row = tasks._row(conn, child_id)
        child_req = json.loads(child_row["request"])
        child_pub = tasks._public(child_row)
        child_res = child_pub.get("result") if isinstance(child_pub.get("result"), dict) else {}
        if isinstance(child_res.get("expires_at"), (int, float)):
            last_expires = max(last_expires, float(child_res["expires_at"]))
        stages.append(_stage_entry(stage, child_row, child_pub))

        if child_row["cancel"] or child_pub["state"] == "cancelled":
            state, error = "cancelled", child_pub["error_code"] or "ENVIRONMENT_TASK_CANCELLED"
            break
        if child_row["state"] == "blocked" or child_pub["state"] == "blocked":
            state, error = "blocked", child_pub["error_code"] or "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
            break
        # 原始落库 state == "observed" 且已通过上方同摘要无漂移校验即可推进下一阶段
        if child_row["state"] == "observed":
            continue
        # 排队中的 candidate-install 在未派发前若上传余量不足 600s，续签同摘要计划窗，不改已派发任务。
        if (
            stage == "candidate-install"
            and child_row["state"] == "waiting"
            and not child_row["cancel"]
            and not child_req.get("attempted")
            and not child_req.get("dispatched")
            and float(child_row["lease_until"]) <= time.time()
            and float(child_req["plan"]["expires_at"]) - time.time() < _INSTALL_PRE_DISPATCH_FLOOR
        ):
            refresh_install_id = child_id
            break
        if child_pub["state"] == "expired":
            state, error = "blocked", "ENVIRONMENT_TASK_PLAN_EXPIRED"
            break
        # 仍在队列/运行/核对中：等待下轮 worker 唤醒
        break

    return {
        "sequence": sequence,
        "stages": stages,
        "state": state,
        "error": error,
        "last_expires": last_expires,
        "next_stage": next_stage,
        "refresh_install_id": refresh_install_id,
    }


def _apply_site_preflight_gate(conn: Any, row: Any, request: dict[str, Any], prog: dict[str, Any]) -> None:
    if prog["state"] != "waiting" or prog["next_stage"] != "candidate-install":
        return
    prep_row = tasks._row(conn, request["children"]["candidate-prepare"])
    prep_req = json.loads(prep_row["request"])
    refresh_site_id = request["children"].get("site-preflight-refresh")
    latest_site_id = refresh_site_id or prep_req["children"]["site-preflight"]
    latest_site = tasks._row(conn, latest_site_id)
    latest_site_pub = tasks._public(latest_site)
    if refresh_site_id:
        if latest_site["cancel"] or latest_site_pub["state"] == "cancelled":
            prog["state"], prog["error"], prog["next_stage"] = "cancelled", "ENVIRONMENT_TASK_CANCELLED", None
        elif latest_site["state"] == "blocked" or latest_site_pub["state"] == "blocked":
            prog["state"] = "blocked"
            prog["error"] = latest_site_pub["error_code"] or "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
            prog["next_stage"] = None
        elif latest_site["state"] != "observed":
            prog["next_stage"] = None
        else:
            prog["site_preflight_override"] = refresh_site_id
    if prog["state"] == "waiting" and prog["next_stage"] == "candidate-install":
        baseline = _site_baseline_status(
            conn,
            row["environment"],
            latest_site,
            exclude_id=prep_req["children"].get("site-install"),
        )
        if baseline == "busy":
            prog["next_stage"] = None
        elif baseline == "stale":
            prog["need_site_refresh"] = True
            prog["next_stage"] = None


def _renew_candidate_install_window(
    path: str,
    reference: str,
    identifier: str,
    refresh_install_id: str,
    request: dict[str, Any],
    identity: tuple[str, str],
) -> None:
    fresh_plan = _renew_same_digest_plan(
        path,
        identity,
        request["plan"],
        reference,
        min_remaining_seconds=_STAGE_MIN_REMAINING["candidate-install"],
    )
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        parent_now = tasks._row(conn, identifier)
        child_now = tasks._row(conn, refresh_install_id)
        child_req_now = json.loads(child_now["request"])
        child_res_now = json.loads(child_now["result"])
        if (
            parent_now["state"] == "waiting"
            and not parent_now["cancel"]
            and child_now["state"] == "waiting"
            and not child_now["cancel"]
            and not child_req_now.get("attempted")
            and not child_req_now.get("dispatched")
            and float(child_now["lease_until"]) <= time.time()
        ):
            child_req_now["plan"] = fresh_plan
            child_res_now["expires_at"] = fresh_plan["expires_at"]
            conn.execute(
                "UPDATE environment_tasks SET request=?,result=?,updated_at=? WHERE id=?",
                (
                    json.dumps(child_req_now),
                    json.dumps(child_res_now),
                    time.time(),
                    refresh_install_id,
                ),
            )
            parent_req_now = json.loads(parent_now["request"])
            parent_req_now["plan"] = fresh_plan
            conn.execute(
                "UPDATE environment_tasks SET request=?,updated_at=? WHERE id=?",
                (json.dumps(parent_req_now), time.time(), identifier),
            )


def _dispatch_site_preflight_refresh(
    path: str, reference: str, identifier: str, request: dict[str, Any], identity: tuple[str, str]
) -> None:
    fresh_plan = _renew_same_digest_plan(
        path,
        identity,
        request["plan"],
        reference,
        min_remaining_seconds=_FRESH_SITE_MIN_REMAINING,
        force_new=True,
    )
    site_task = tasks.submit(path, identity, fresh_plan["id"], reference)
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        parent_now = tasks._row(conn, identifier)
        stored = json.loads(parent_now["request"])
        if parent_now["state"] == "waiting" and not parent_now["cancel"]:
            stored["children"]["site-preflight-refresh"] = site_task["id"]
            stored["plan"] = fresh_plan
            conn.execute(
                "UPDATE environment_tasks SET request=?,updated_at=? WHERE id=?",
                (json.dumps(stored), time.time(), identifier),
            )


def _spawn_next_stage_child(
    context: Any,
    path: str,
    reference: str,
    identifier: str,
    request: dict[str, Any],
    identity: tuple[str, str],
    next_stage: str,
    site_preflight_override: str | None,
) -> None:
    # 释放 DB 写事务后，按阶段余量续签同摘要计划并在子任务写锁内原子核验父未取消后绑定子任务。
    fresh_plan = _renew_same_digest_plan(
        path,
        identity,
        request["plan"],
        reference,
        min_remaining_seconds=_STAGE_MIN_REMAINING.get(next_stage, 600.0),
    )
    if next_stage == "candidate-install":
        child = installation.submit(
            context,
            identity,
            request["children"]["candidate-prepare"],
            plan_id=fresh_plan["id"],
            onboard_parent=identifier,
            site_preflight_id=site_preflight_override,
        )
    elif next_stage == "candidate-register":
        child = registration.submit(
            context,
            identity,
            request["children"]["candidate-install"],
            fresh_plan["id"],
            onboard_parent=identifier,
        )
    elif next_stage == "candidate-validate":
        child = validation.submit(
            context,
            identity,
            {
                "registration_task": request["children"]["candidate-register"],
                "validation_plan": fresh_plan["id"],
                "capabilities": request["capabilities"],
                "case_ref": request["case_ref"],
            },
            onboard_parent=identifier,
        )
    else:
        return
    if child is None:
        return

    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row = tasks._row(conn, identifier)
        stored = json.loads(row["request"])
        if row["state"] != "waiting" or row["cancel"]:
            return
        stages_now = []
        for s in _stage_sequence(stored):
            cid = stored["children"].get(s)
            if not cid:
                break
            crow = tasks._row(conn, cid)
            stages_now.append(_stage_entry(s, crow, tasks._public(crow)))
        _record_parent(
            conn,
            identifier,
            stored,
            stages_now,
            state="waiting",
            error="",
            expires_at=float(stored["plan"]["expires_at"]),
            ready_to_publish=False,
        )


def _map_advance_block_code(exc: Exception) -> str | None:
    raw = str(exc)
    if isinstance(exc, (tasks.TaskRefused, plans.PlanRefused, registry.RegistryError)):
        if raw == "ENVIRONMENT_TASK_BUSY":
            # 另一条环境安装任务正在占用互斥车道：保留 waiting，下轮 worker 唤醒自然续跑。
            return None
        if raw == "ENVIRONMENT_MANAGEMENT_FORBIDDEN":
            return "ENVIRONMENT_MANAGEMENT_FORBIDDEN"
        if raw in (
            "ENVIRONMENT_PLAN_REVISION_CONFLICT",
            "ENVIRONMENT_DRAFT_UNAVAILABLE",
            "ENVIRONMENT_PLAN_UNAVAILABLE",
        ) or raw.endswith("EXPIRED"):
            return "ENVIRONMENT_TASK_PLAN_EXPIRED"
        if isinstance(exc, tasks.TaskRefused):
            return raw
        return "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
    return (
        "ENVIRONMENT_TASK_PLAN_EXPIRED"
        if raw.endswith("EXPIRED")
        else "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
    )


def _advance_one(context: Any, path: str, reference: str, identifier: str) -> None:
    settings = context.settings
    try:
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            request = json.loads(row["request"])
            if request.get("action") != ACTION or row["state"] not in ("waiting", "checking"):
                return
            identity = tuple(request["identity"])
            if row["cancel"]:
                _record_cancelled_parent(conn, identifier, request)
                return
            if row["state"] != "waiting":
                return
            tasks._authorize(path, identity, row["environment"])
            # 复核输入摘要与依赖绑定未漂移（时间窗到期由同摘要续签处理）。
            tasks._current(path, request, reference, reconciling=True)
            if request["bindings"] != preparation._bindings(settings, request["consumer_source"]):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
            if installation.binding(settings, request["target_id"])[1] != request["node_binding"]:
                raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")

            prog = _inspect_stage_progression(conn, request)
            prog["need_site_refresh"] = False
            prog["site_preflight_override"] = None
            _apply_site_preflight_gate(conn, row, request, prog)

            all_done = (
                prog["state"] == "waiting"
                and prog["next_stage"] is None
                and not prog["need_site_refresh"]
                and prog["refresh_install_id"] is None
                and len(prog["stages"]) == len(prog["sequence"])
                and all(
                    tasks._row(conn, request["children"][s])["state"] == "observed" for s in prog["sequence"]
                )
            )
            ready_to_publish = bool(
                all_done
                and request.get("case_ref")
                and request.get("capabilities") == ["consumer", "offline", "order"]
            )
            state = "observed" if all_done else prog["state"]
            _record_parent(
                conn,
                identifier,
                request,
                prog["stages"],
                state=state,
                error=prog["error"],
                expires_at=prog["last_expires"],
                ready_to_publish=ready_to_publish,
            )
            should_spawn = state == "waiting" and prog["next_stage"] is not None

        if prog["refresh_install_id"] is not None:
            _renew_candidate_install_window(
                path, reference, identifier, prog["refresh_install_id"], request, identity
            )
            return
        if prog["need_site_refresh"]:
            _dispatch_site_preflight_refresh(path, reference, identifier, request, identity)
            return
        if should_spawn and prog["next_stage"] is not None:
            _spawn_next_stage_child(
                context,
                path,
                reference,
                identifier,
                request,
                identity,
                prog["next_stage"],
                prog["site_preflight_override"],
            )
    except (
        tasks.TaskRefused,
        plans.PlanRefused,
        registry.RegistryError,
        ValueError,
        RuntimeError,
        OSError,
    ) as exc:
        code = _map_advance_block_code(exc)
        if code is None:
            return
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            if row["state"] == "waiting" and not row["cancel"]:
                conn.execute(
                    "UPDATE environment_tasks SET state='blocked',error=?,updated_at=? WHERE id=?",
                    (code, time.time(), identifier),
                )
