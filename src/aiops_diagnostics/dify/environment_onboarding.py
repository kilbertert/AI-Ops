"""一键租户开通编排：沿既有受控原子阶段自动串联准备、安装、登记与可选验证；绝不自动启用正式准入。"""

from __future__ import annotations

import json
import time
from typing import Any

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.environment_business import CAPABILITIES

from . import environment_candidate_preparation as preparation
from . import environment_installation as installation
from . import environment_management as management
from . import environment_registration as registration
from . import environment_validation as validation

ACTION = "tenant-onboard"
BASE_STAGES = ("candidate-prepare", "candidate-install", "candidate-register")
ALL_STAGES = (*BASE_STAGES, "candidate-validate")
_RENEW_MIN_REMAINING_SECONDS = 300.0


def _stage_sequence(request: dict[str, Any]) -> tuple[str, ...]:
    return ALL_STAGES if request.get("case_ref") else BASE_STAGES


def _renew_same_digest_plan(
    path: str, identity: tuple[str, str, str], original_plan: dict[str, Any], reference: str
) -> dict[str, Any]:
    """仅当草稿、目录、制品引用与授权完全未漂移时续签同摘要计划窗，防止慢链误超时。"""
    tasks._authorize(path, identity, original_plan["environment"])
    fresh = plans.preflight(
        path,
        identity,
        original_plan["environment"],
        original_plan["draft_revision"],
        original_plan["registry_revision"],
        reference,
        min_remaining_seconds=_RENEW_MIN_REMAINING_SECONDS,
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

    # 先校验是否已存在相同幂等任务或同租户并发在飞一键开通任务。
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

    prep_payload: dict[str, Any] = {
        "candidate_plan": plan["id"],
        "tenant_id": tenant_id,
        "source_app_id": payload["source_app_id"],
        "source_version": payload["source_version"],
    }
    if initial:
        prep_payload["initial_installation"] = True
    prep_task = preparation.submit(context, identity, prep_payload, source, onboard_parent=identifier)

    request: dict[str, Any] = {
        "identity": list(identity),
        "action": ACTION,
        "tenant_id": tenant_id,
        "plan": plan,
        "target_id": target_id,
        "consumer_source": source,
        "bindings": bindings,
        "node_binding": node_binding,
        "attempted": True,
        "children": {"candidate-prepare": prep_task["id"]},
        **({"initial_installation": True} if initial else {}),
        **({"case_ref": case_ref, "capabilities": capabilities} if case_ref else {}),
    }
    initial_result: dict[str, Any] = {
        "scope": "tenant-one-click-onboarding",
        "executable": False,
        "expires_at": plan["expires_at"],
        "stages": [
            {
                "action": "candidate-prepare",
                "id": prep_task["id"],
                "state": prep_task["state"],
                "error_code": prep_task["error_code"],
            }
        ],
        "ready_to_publish": False,
        "activation": "not_executed",
        "business_accuracy": "unconfirmed",
        "validation_task_id": "",
        "registration_task_id": "",
        "installation_task_id": "",
        "preparation_task_id": prep_task["id"],
    }
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? "
            "AND state IN ('waiting','checking') AND id!=? "
            "AND json_extract(request,'$.action')=? AND json_extract(request,'$.tenant_id')=? LIMIT 1",
            (environment, identifier, ACTION, tenant_id),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (identifier, environment, json.dumps(request), json.dumps(initial_result), time.time()),
        )
        return tasks._public(tasks._row(conn, identifier))


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
                stages_cancel: list[dict[str, Any]] = []
                last_cancel_exp = float(request["plan"]["expires_at"])
                for stage in _stage_sequence(request):
                    child_id = request["children"].get(stage)
                    if not child_id:
                        break
                    child_row = tasks._row(conn, child_id)
                    child_pub = tasks._public(child_row)
                    child_res = child_pub.get("result") if isinstance(child_pub.get("result"), dict) else {}
                    if isinstance(child_res.get("expires_at"), (int, float)):
                        last_cancel_exp = max(last_cancel_exp, float(child_res["expires_at"]))
                    stages_cancel.append(_stage_entry(stage, child_row, child_pub))
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

            sequence = _stage_sequence(request)
            stages: list[dict[str, Any]] = []
            state, error = "waiting", ""
            last_expires = float(request["plan"]["expires_at"])
            next_stage: str | None = None

            for stage in sequence:
                child_id = request["children"].get(stage)
                if not child_id:
                    next_stage = stage
                    break
                child_row = tasks._row(conn, child_id)
                child_pub = tasks._public(child_row)
                child_res = child_pub.get("result") if isinstance(child_pub.get("result"), dict) else {}
                if isinstance(child_res.get("expires_at"), (int, float)):
                    last_expires = max(last_expires, float(child_res["expires_at"]))
                stages.append(_stage_entry(stage, child_row, child_pub))

                if child_row["cancel"] or child_pub["state"] == "cancelled":
                    state, error = "cancelled", child_pub["error_code"] or "ENVIRONMENT_TASK_CANCELLED"
                    break
                if child_row["state"] == "blocked" or child_pub["state"] == "blocked":
                    state, error = (
                        "blocked",
                        child_pub["error_code"] or "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
                    )
                    break
                # 原始落库 state == "observed" 且已通过上方同摘要无漂移校验即可推进下一阶段
                if child_row["state"] == "observed":
                    continue
                if child_pub["state"] == "expired":
                    state, error = "blocked", "ENVIRONMENT_TASK_PLAN_EXPIRED"
                    break
                # 仍在队列/运行/核对中：等待下轮 worker 唤醒
                break

            all_done = (
                state == "waiting"
                and next_stage is None
                and len(stages) == len(sequence)
                and all(tasks._row(conn, request["children"][s])["state"] == "observed" for s in sequence)
            )
            ready_to_publish = bool(
                all_done
                and request.get("case_ref")
                and request.get("capabilities") == ["consumer", "offline", "order"]
            )
            if all_done:
                state = "observed"
            _record_parent(
                conn,
                identifier,
                request,
                stages,
                state=state,
                error=error,
                expires_at=last_expires,
                ready_to_publish=ready_to_publish,
            )
            should_spawn = state == "waiting" and next_stage is not None

        if not should_spawn or next_stage is None:
            return

        # 释放 DB 写事务后，按同摘要续签计划并在子任务写锁内原子核验父未取消后绑定子任务。
        fresh_plan = _renew_same_digest_plan(path, identity, request["plan"], reference)
        if next_stage == "candidate-install":
            child = installation.submit(
                context,
                identity,
                request["children"]["candidate-prepare"],
                plan_id=fresh_plan["id"],
                onboard_parent=identifier,
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
    except (tasks.TaskRefused, plans.PlanRefused, registry.RegistryError) as exc:
        raw = str(exc)
        if raw == "ENVIRONMENT_TASK_BUSY":
            # 另一条环境安装任务正在占用互斥车道：保留 waiting，下轮 worker 唤醒自然续跑。
            return
        if raw == "ENVIRONMENT_MANAGEMENT_FORBIDDEN":
            code = "ENVIRONMENT_MANAGEMENT_FORBIDDEN"
        elif raw in (
            "ENVIRONMENT_PLAN_REVISION_CONFLICT",
            "ENVIRONMENT_DRAFT_UNAVAILABLE",
            "ENVIRONMENT_PLAN_UNAVAILABLE",
        ) or raw.endswith("EXPIRED"):
            code = "ENVIRONMENT_TASK_PLAN_EXPIRED"
        elif isinstance(exc, tasks.TaskRefused):
            code = raw
        else:
            code = "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            if row["state"] == "waiting" and not row["cancel"]:
                conn.execute(
                    "UPDATE environment_tasks SET state='blocked',error=?,updated_at=? WHERE id=?",
                    (code, time.time(), identifier),
                )
    except (ValueError, RuntimeError, OSError) as exc:
        code = (
            "ENVIRONMENT_TASK_PLAN_EXPIRED"
            if str(exc).endswith("EXPIRED")
            else "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
        )
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            if row["state"] == "waiting" and not row["cancel"]:
                conn.execute(
                    "UPDATE environment_tasks SET state='blocked',error=?,updated_at=? WHERE id=?",
                    (code, time.time(), identifier),
                )
