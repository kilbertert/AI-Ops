"""已登记候选的实际三业务验证；任务回执不是正式准入许可。"""

import json
import time

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.environment_business import CAPABILITIES

from . import environment_management as management
from . import environment_registration as registration

ACTION = "candidate-validate"


def current(context, conn, identifier, *, fresh=False):
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    path = management._path(context)
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    tasks._current(path, request, context.settings.environment_onboarding_file, reconciling=not fresh)
    source, original = registration.current(context, conn, request["registration_task"])
    if (
        source["state"] != "observed"
        or source["cancel"]
        or original["installed"] != request["installed"]
        or fresh
        and row["cancel"]
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
    return row, request


def submit(context, identity, payload):
    capabilities, reference = payload["capabilities"], payload["case_ref"]
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(not isinstance(c, str) for c in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or not set(capabilities) <= set(CAPABILITIES)
        or not isinstance(reference, str)
        or not drafts.REFERENCE_PATTERN.fullmatch(reference)
    ):
        raise tasks.TaskRefused("ENVIRONMENT_VALIDATION_CASE_INVALID")
    path = management._path(context)
    plan = plans.read(
        path, identity, payload["validation_plan"], context.settings.environment_onboarding_file
    )
    if plan["state"] == "expired":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        source = tasks._row(conn, payload["registration_task"])
        original = json.loads(source["request"])
        tasks._authorize(path, identity, source["environment"])
        if (
            original.get("action") != registration.ACTION
            or source["state"] != "observed"
            or source["cancel"]
            or original["plan"]["digest"] != plan["digest"]
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        installation = tasks._row(conn, original["installation_task"])
        installed = original["installed"]
        if tuple(map(int, installed["version"].split("+")[0].split("."))) < (1, 36, 0):
            raise tasks.TaskRefused("ENVIRONMENT_VALIDATION_UPGRADE_REQUIRED")
        identifier = (
            "task_" + plans._digest([ACTION, source["id"], plan["id"], sorted(capabilities), reference])[:32]
        )
        request = {
            **{k: original[k] for k in ("target", "installer", "tenant_id", "installed")},
            "registration_task": source["id"],
            "plan": plan,
            "identity": identity,
            "action": ACTION,
            "capabilities": sorted(capabilities),
            "case_ref": reference,
            "attempted": False,
            "children": json.loads(installation["request"])["children"],
            "steps": {},
        }
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
            "AND state IN ('waiting','executing','checking') "
            "AND json_extract(request,'$.action') "
            "IN ('candidate-install','candidate-register','candidate-validate','environment-publish')",
            (source["environment"], identifier),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        result = {
            "scope": "installed-business-validation-only",
            "expires_at": plan["expires_at"],
            "activated": False,
            "business_accuracy": "unconfirmed",
            "checks": [dict(capability=c, state="pending") for c in sorted(capabilities)],
        }
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (identifier, source["environment"], json.dumps(request), json.dumps(result), time.time()),
        )
        current(context, conn, identifier, fresh=True)
        return tasks._public(tasks._row(conn, identifier))


def dispatch(context, payload):
    if set(payload) not in (
        {"validation", "id", "owner"},
        {"validation", "id", "owner", "capability"},
        {"validation", "id", "owner", "capability", "result"},
    ) or payload.get("validation") not in {"stage", "step", "complete", "continue"}:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    path = management._path(context)
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row = tasks._row(conn, payload["id"])
        request = json.loads(row["request"])
        if (
            request.get("action") != ACTION
            or row["state"] != "executing"
            or row["owner"] != payload["owner"]
            or row["lease_until"] <= time.time()
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        if payload["validation"] == "continue":
            current(context, conn, row["id"], fresh=True)
            return {"authorized": True}
        if payload["validation"] == "complete":
            result = json.loads(row["result"])
            checks = result["checks"]
            result["state"] = (
                "cancelled"
                if row["cancel"]
                else "observed"
                if all(c["state"] == "passed" for c in checks)
                else "blocked"
            )
            conn.execute(
                "UPDATE environment_tasks SET state=?,result=?,owner='',lease_until=0,updated_at=? "
                "WHERE id=?",
                (result["state"], json.dumps(result), time.time(), row["id"]),
            )
            return tasks._public(tasks._row(conn, row["id"]))
        if payload["validation"] == "step":
            capability = payload.get("capability")
            if capability not in request["capabilities"]:
                raise tasks.TaskRefused("ENVIRONMENT_VALIDATION_CASE_INVALID")
            steps = request["steps"]
            if "result" not in payload:
                current(context, conn, row["id"], fresh=True)
                if capability in steps:
                    return {"execute": False, "state": steps[capability]["state"]}
                steps[capability] = {"state": "started"}
                request["attempted"] = True
                conn.execute(
                    "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"])
                )
                return {"execute": True}
            result = payload["result"]
            if (
                capability not in steps
                or steps[capability]["state"] != "started"
                or not isinstance(result, dict)
                or result.get("capability") != capability
                or result.get("state") not in {"passed", "blocked"}
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            from aiops_diagnostics.gateway.environment_business import validate_result

            try:
                validate_result(result, capability)
            except ValueError as exc:
                raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID") from exc
            steps[capability] = result
            public = json.loads(row["result"])
            public["checks"] = [
                steps.get(c, dict(capability=c, state="pending")) for c in request["capabilities"]
            ]
            public["installed"] = {
                k: request["installed"][k]
                for k in ("source_revision", "config_sha256", "registry_sha256", "runtime_sha256")
            }
            conn.execute(
                "UPDATE environment_tasks SET request=?,result=?,updated_at=? WHERE id=?",
                (json.dumps(request), json.dumps(public), time.time(), row["id"]),
            )
            return {"recorded": True}
        current(context, conn, row["id"], fresh=not request["attempted"])
        operator = tasks._row(conn, request["children"]["operator-prepare"])
        consumer_task = request["children"]["consumer-prepare"]
        consumer_request = json.loads(tasks._row(conn, consumer_task)["request"])
        consumer_result = json.loads(tasks._row(conn, consumer_task)["result"])
    import yaml

    from .environment_consumer import document
    from .environment_consumer_trial import candidate_workflow_sha

    scope = {k: consumer_request[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id")}
    scope.update(
        environment=row["environment"],
        app_id=consumer_result["app_id"],
        task_id=consumer_task,
        plan_id=request["plan"]["id"],
        order_no="",
        workflow_sha256=candidate_workflow_sha(
            yaml.safe_dump(document(consumer_request["consumer_source"], scope["knowledge_base_id"]))
        ),
    )
    return {
        "target": request["target"],
        "spec": {
            "id": row["id"],
            "environment": row["environment"],
            "tenant_id": request["tenant_id"],
            "case_ref": request["case_ref"],
            "capabilities": request["capabilities"],
            "installed": request["installed"],
            "operator_app": json.loads(operator["result"])["app_id"],
            "consumer": scope,
        },
        "steps": request["steps"],
        "cancelled": bool(row["cancel"]),
    }
