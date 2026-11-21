"""把已安装候选接到原登记事务；未知结果只读观察，不重装或激活。"""

import json
import re
import time

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db

from . import environment_installation as installation
from . import environment_management as management

ACTION = "candidate-register"


def current(context, conn, identifier, *, fresh=False):
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    # 已派发请求只用冻结目标对账，撤权/草稿变化不能把未知结果变成安全终态。
    if not fresh and request.get("dispatched"):
        return row, request
    path = management._path(context)
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    tasks._current(path, request, context.settings.environment_onboarding_file, reconciling=not fresh)
    source, original = installation.current(context, conn, request["installation_task"])
    result = json.loads(source["result"])
    if (
        original["action"] != installation.ACTION
        or source["state"] != "observed"
        or source["cancel"]
        or result != request["installed"]
        or original["plan"]["digest"] != request["plan"]["digest"]
        or fresh
        and row["cancel"]
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
    return row, request


def submit(context, identity, identifier, plan_id):
    path, settings = management._path(context), context.settings
    plan = plans.read(path, identity, plan_id, settings.environment_onboarding_file)
    if plan["state"] == "expired":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        source = tasks._row(conn, identifier)
        tasks._authorize(path, identity, source["environment"])
        original = json.loads(source["request"])
        result = json.loads(source["result"])
        if (
            original.get("action") != installation.ACTION
            or source["state"] != "observed"
            or source["cancel"]
            or plan["environment"] != source["environment"]
            or plan["digest"] != original["plan"]["digest"]
            or result.get("installation") not in {"installed", "unchanged"}
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        version = result.get("version", "").split("+")[0].split(".")
        if (
            len(version) != 3
            or not all(v.isdigit() for v in version)
            or tuple(map(int, version)) < (1, 35, 0)
        ):
            raise tasks.TaskRefused("ENVIRONMENT_REGISTRATION_UPGRADE_REQUIRED")
        task_id = "task_" + plans._digest([ACTION, identifier, plan_id])[:32]
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
            "AND state IN ('waiting','executing','checking') "
            "AND json_extract(request,'$.action') IN ('candidate-install','candidate-register')",
            (source["environment"], task_id),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        request = {
            **{k: original[k] for k in ("target", "installer", "tenant_id")},
            "identity": identity,
            "plan": plan,
            "action": ACTION,
            "installation_task": identifier,
            "installed": result,
            "attempted": False,
        }
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (
                task_id,
                source["environment"],
                json.dumps(request),
                json.dumps(
                    {
                        "scope": "inactive-runtime-registration-only",
                        "result": "not_executed",
                        "expires_at": plan["expires_at"],
                        "activated": False,
                        "business_acceptance": False,
                    }
                ),
                time.time(),
            ),
        )
        current(context, conn, task_id, fresh=True)
        return tasks._public(tasks._row(conn, task_id))


def dispatch(context, payload):
    path = management._path(context)
    owner = payload.get("owner")
    if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    if set(payload) not in (
        {"registration", "id", "owner"},
        {"registration", "id", "owner", "result"},
    ) or payload.get("registration") not in {"stage", "authorize", "complete"}:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row = tasks._row(conn, payload["id"])
        request = json.loads(row["request"])
        if (
            request.get("action") != ACTION
            or row["state"] != "executing"
            or row["owner"] != owner
            or row["lease_until"] <= time.time()
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        action = payload["registration"]
        if action == "complete":
            result = payload.get("result")
            expected = {
                "id": row["id"],
                "environment": row["environment"],
                "tenant_id": request["tenant_id"],
                "scope": "inactive-runtime-registration-only",
                "executable": False,
                "activated": False,
                "business_acceptance": False,
                **{
                    k: request["installed"][k]
                    for k in (
                        "source_revision",
                        "artifact_sha256",
                        "config_sha256",
                        "registry_sha256",
                        "runtime_sha256",
                    )
                },
            }
            if (
                not isinstance(result, dict)
                or set(result) != set(expected) | {"state", "result"}
                or any(type(result[k]) is not type(v) or result[k] != v for k, v in expected.items())
                or result["state"] not in {"observed", "reconcile"}
                or result["result"]
                not in (
                    {"registered", "unchanged"}
                    if result["state"] == "observed"
                    else {"absent", "unconfirmed"}
                )
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            state = (
                "checking" if result["state"] == "reconcile" else "cancelled" if row["cancel"] else "observed"
            )
            # 未派发且只读确认不存在，安全结束；已派发的未知请求不能提前释放互斥。
            if result["result"] == "absent" and not request.get("dispatched"):
                state = "cancelled" if row["cancel"] else "blocked"
            conn.execute(
                "UPDATE environment_tasks SET state=?,result=?,owner='',lease_until=0,updated_at=? "
                "WHERE id=?",
                (
                    state,
                    json.dumps({**result, "expires_at": request["plan"]["expires_at"]}),
                    time.time(),
                    row["id"],
                ),
            )
            return tasks._public(tasks._row(conn, row["id"]))
        current(context, conn, row["id"], fresh=action == "authorize" or not request["attempted"])
        if action == "authorize":
            if not request["attempted"] or request.get("dispatched"):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
            request["dispatched"] = True
            conn.execute(
                "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"])
            )
            return {"authorized": True}
        attempted = request["attempted"]
        request["attempted"] = True
        conn.execute("UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"]))
        return {
            "id": row["id"],
            "environment": row["environment"],
            "tenant_id": request["tenant_id"],
            "target": request["target"],
            "installer": request["installer"],
            "expires_at": request["plan"]["expires_at"],
            "observe_only": attempted,
            "expected": {
                "revision": request["installed"]["source_revision"],
                **{
                    k: request["installed"][k]
                    for k in ("artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256")
                },
            },
        }
