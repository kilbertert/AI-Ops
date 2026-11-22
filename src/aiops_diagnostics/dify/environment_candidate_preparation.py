"""一次提交的停用候选准备；资源动作仍由既有持久子任务执行。"""

import json
import time

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db

from . import environment_consumer, environment_management, environment_operator
from .environment_knowledge_connection import binding as connection_binding
from .self_service_identity import SetupRefused

ACTION = "candidate-prepare"
STEPS = (
    "site-preflight",
    "workspace-prepare",
    "knowledge-prepare",
    "knowledge-connect",
    "operator-prepare",
    "consumer-prepare",
)


def _bindings(settings, source):
    return {
        "workspace": plans._digest(
            {
                k: getattr(settings, k, "")
                for k in (
                    "dify_inner_api_base_url",
                    "dify_inner_api_key",
                    "dify_console_base_url",
                    "dify_console_api_key",
                )
            }
        ),
        "knowledge": environment_management.knowledge_binding(settings),
        "knowledge_target": environment_management.knowledge_binding(settings, target_only=True),
        "connection": connection_binding(settings),
        "operator": environment_operator.binding(settings),
        "consumer": environment_consumer.binding(settings, source),
    }


def submit(context, identity, payload, source):
    settings = context.settings
    if not isinstance(payload["tenant_id"], str) or not drafts.REFERENCE_PATTERN.fullmatch(
        payload["tenant_id"]
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
    if not all(
        getattr(settings, k, "")
        for k in (
            "environment_task_key",
            "dify_inner_api_base_url",
            "dify_inner_api_key",
            "dify_console_base_url",
            "dify_console_api_key",
            "kb_service_base_url",
            "ragflow_credential_key",
            "ragflow_embedding_base_url",
            "ragflow_embedding_api_key",
            "internal_token_secret",
            "dify_knowledge_api_key",
            "operator_profiles_file",
        )
    ):
        raise SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
    path, reference = environment_management._path(context), settings.environment_onboarding_file
    bindings = _bindings(settings, source)
    plan = plans.read(path, identity, payload["candidate_plan"], reference)
    # 「包含初装」只能显式声明；此时首个子任务是受管冷装而非只读预检。
    initial = payload.get("initial_installation") is True
    with drafts._locked(path):
        draft = next(
            r for r in drafts._rows(drafts._database(path)) if r["environment"] == plan["environment"]
        )
        if not any(t["id"] == payload["tenant_id"] for t in json.loads(draft["body"])["tenants"]):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
    site = None if initial else tasks.submit(path, identity, payload["candidate_plan"], reference)
    environment = plan["environment"]
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        # 首个控制任务可能就是本环境首行；原初始化必须先于任何任务表查询。
        drafts.initialize_tasks(conn)
        if initial:
            if plan["state"] == "expired" or any(
                c["state"] != "passed"
                for c in plan["checks"]
                if c["id"] not in {"host-identity", "capacity", "network", "business"}
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_BLOCKED")
            proposed = next(
                r for r in drafts._rows(drafts._database(path)) if r["environment"] == environment
            )
            target = next(
                t
                for t in plans._references(reference)
                if t["id"] == json.loads(proposed["body"])["deployment_target"]
            )
            request = {
                "identity": identity,
                "plan": plan,
                "target": {k: target[k] for k in ("id", "host", "user", "port")},
                "installer": {k: target["installer"][k] for k in ("sha256", "revision")},
            }
        else:
            request = json.loads(tasks._row(conn, site["id"])["request"])
        tasks._authorize(path, identity, environment)
        tasks._current(path, request, reference)
        draft = next(r for r in drafts._rows(drafts._database(path)) if r["environment"] == environment)
        tenants = json.loads(draft["body"])["tenants"]
        if not any(t["id"] == payload["tenant_id"] for t in tenants):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
        identifier = (
            "task_"
            + plans._digest(
                {
                    "action": ACTION,
                    "plan": plan["id"],
                    "tenant": payload["tenant_id"],
                    "source": source,
                    "bindings": bindings,
                    **({"initial_installation": True} if initial else {}),
                }
            )[:32]
        )
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? AND state='waiting' AND id!=? "
            "AND json_extract(request,'$.action')=? AND json_extract(request,'$.tenant_id')=? "
            "AND json_extract(request,'$.plan.id')=? LIMIT 1",
            (environment, identifier, ACTION, payload["tenant_id"], plan["id"]),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        request.update(
            identity=identity,
            action=ACTION,
            tenant_id=payload["tenant_id"],
            consumer_source=source,
            bindings=bindings,
            children={},
            **({"initial_installation": True} if initial else {}),
        )
        if initial:
            from .environment_installation import submit_cold

            request["children"]["site-install"] = submit_cold(
                conn, environment, identity, identifier, request
            )
        else:
            request["children"]["site-preflight"] = site["id"]
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (
                identifier,
                environment,
                json.dumps(request),
                json.dumps(
                    {
                        "scope": "candidate-resources-only",
                        "expires_at": request["plan"]["expires_at"],
                        "steps": [],
                        "installation": "not_executed",
                        **({"initial_installation": "not_executed"} if initial else {}),
                        "business": "unverified",
                        "activation": "not_executed",
                    }
                ),
                time.time(),
            ),
        )
        return tasks._public(tasks._row(conn, identifier))


def authorize_parent(conn, path, reference, identifier, settings):
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    if request.get("action") != ACTION or row["cancel"] or row["state"] != "waiting":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_CANCELLED")
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    tasks._current(path, request, reference)
    if request["bindings"] != _bindings(settings, request["consumer_source"]):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    return request


def advance(context, payload):
    if payload != {"advance_candidates": True}:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    path = environment_management._path(context)
    reference = context.settings.environment_onboarding_file
    if not drafts._database(path).exists():
        return {"advanced": 0}
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
            return {"advanced": 0}
        identifiers = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM environment_tasks WHERE state='waiting' "
                "AND json_extract(request,'$.action')=? ORDER BY updated_at LIMIT 20",
                (ACTION,),
            )
        ]
    for identifier in identifiers:
        _advance_one(context, path, reference, identifier)
    return {"advanced": len(identifiers)}


def _advance_one(context, path, reference, identifier):
    try:
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            request = authorize_parent(conn, path, reference, identifier, context.settings)
            sequence = (
                ["site-install", *STEPS] if request.get("initial_installation") is True else list(STEPS)
            )
            steps = []
            state, error = "waiting", ""
            initial_installation = None
            for action in sequence:
                if action not in request["children"]:
                    break
                child = tasks._public(tasks._row(conn, request["children"][action]))
                steps.append({k: child[k] for k in ("id", "action", "state", "error_code")})
                if action == "site-install":
                    initial_installation = child["result"].get("installation", "not_executed")
                if child["cancel_requested"] or (
                    child["state"] in tasks.FINISHED_STATES and child["state"] != "observed"
                ):
                    state, error = "blocked", child["error_code"] or "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"
                    break
                if child["state"] != "observed":
                    break
            if len(steps) == len(sequence) and all(s["state"] == "observed" for s in steps):
                state = "observed"
            result = {
                "scope": "candidate-resources-only",
                "executable": False,
                "expires_at": request["plan"]["expires_at"],
                "steps": steps,
                "installation": "not_executed",
                "business": "unverified",
                "activation": "not_executed",
            }
            # 基础初装结果独立成字段；父任务自身 runtime 安装字段保持原语义。
            if initial_installation is not None:
                result["initial_installation"] = initial_installation
            conn.execute(
                "UPDATE environment_tasks SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                (state, json.dumps(result), error, time.time(), identifier),
            )
            ready = state == "waiting" and steps and all(s["state"] == "observed" for s in steps)
        if not ready:
            return
        action = sequence[len(steps)]
        if action == "site-preflight" and "site-preflight" not in request["children"]:
            # 初装确认在场后才出现场预检；after绑定令同计划陈旧阻塞行不被复用。
            site = tasks.submit(
                path,
                tuple(request["identity"]),
                request["plan"]["id"],
                reference,
                after=request["children"]["site-install"],
            )
            with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
                row = tasks._row(conn, identifier)
                stored = json.loads(row["request"])
                if (
                    row["state"] == "waiting"
                    and not row["cancel"]
                    and "site-preflight" not in stored["children"]
                ):
                    stored["children"]["site-preflight"] = site["id"]
                    conn.execute(
                        "UPDATE environment_tasks SET request=?,updated_at=? WHERE id=?",
                        (json.dumps(stored), time.time(), identifier),
                    )
            return
        parent = request["children"][
            "knowledge-connect"
            if action in {"operator-prepare", "consumer-prepare"}
            else sequence[len(steps) - 1]
        ]
        bindings = request["bindings"]
        tasks.submit_workspace(
            path,
            tuple(request["identity"]),
            parent,
            request["tenant_id"],
            reference,
            knowledge=action == "knowledge-prepare",
            connect=action == "knowledge-connect",
            application=action in {"operator-prepare", "consumer-prepare"},
            consumer=action == "consumer-prepare",
            consumer_source=request["consumer_source"] if action == "consumer-prepare" else None,
            binding=bindings.get(
                {
                    "knowledge-prepare": "knowledge",
                    "knowledge-connect": "connection",
                    "operator-prepare": "operator",
                    "consumer-prepare": "consumer",
                }.get(action, ""),
                "",
            ),
            target_binding=bindings["knowledge_target"] if action == "knowledge-prepare" else "",
            candidate_parent=(identifier, _bindings(context.settings, request["consumer_source"])),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            row = tasks._row(conn, identifier)
            if row["state"] == "waiting" and not row["cancel"]:
                conn.execute(
                    "UPDATE environment_tasks SET state='blocked',error=?,updated_at=? WHERE id=?",
                    (
                        "ENVIRONMENT_TASK_PLAN_EXPIRED"
                        if str(exc).endswith("EXPIRED")
                        else "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
                        time.time(),
                        identifier,
                    ),
                )
