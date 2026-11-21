"""冻结现场预检任务：持久去重、授权、租约与结果核对；不安装或激活。"""

import json
import math
import re
import time

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import gateway_db

FINISHED_STATES = frozenset({"observed", "blocked", "cancelled", "expired"})


class TaskRefused(ValueError):
    pass


def _row(conn, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"task_[0-9a-f]{32}", identifier):
        raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
    row = conn.execute("SELECT * FROM environment_tasks WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
    return dict(row)


def _authorize(path, identity, environment):
    allowed = drafts.access(registry.load(path), identity)
    if "*" not in allowed and environment not in allowed:
        raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")


def _current(path, request, reference, *, reconciling=False):
    plan = request["plan"]
    digest, _, _ = plans._inputs(
        path,
        tuple(request["identity"]),
        plan["environment"],
        plan["draft_revision"],
        plan["registry_revision"],
        reference,
    )
    if digest != plan["digest"] or (not reconciling and plan["expires_at"] <= time.time()):
        raise TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")


def _public(row):
    request = json.loads(row["request"])
    state = row["state"]
    if state == "waiting" and request.get("action") in {
        "candidate-prepare",
        "candidate-install",
        "candidate-register",
    }:
        state = "queued"
    if state == "executing":
        state = "running" if row["lease_until"] > time.time() else "reconcile"
    if state == "checking":
        state = "reconcile"
    if state == "running" and row["lease_until"] <= time.time():
        state = "reconcile"
    if state == "queued" and request["plan"]["expires_at"] <= time.time():
        state = "expired"
    result = json.loads(row["result"])
    if state in {"observed", "blocked"} and result["expires_at"] <= time.time():
        state = "expired"
    return {
        "id": row["id"],
        "environment": row["environment"],
        "action": request.get("action", "site-preflight"),
        "tenant_id": request.get("tenant_id", ""),
        "plan_id": request["plan"]["id"],
        "state": state,
        "executable": False,
        "cancel_requested": bool(row["cancel"]),
        "result": result,
        "error_code": row["error"],
        "updated_at": row["updated_at"],
    }


VERIFY_CHECKS = frozenset({"managed-host-identity"})


def _plan_task(path, identity, identifier, reference, action_fields):
    plan = plans.read(path, identity, identifier, reference)
    if plan["state"] == "expired" or any(
        c["state"] != "passed"
        for c in plan["checks"]
        if c["id"] not in {"host-identity", "capacity", "network", "business"}
    ):
        raise TaskRefused("ENVIRONMENT_TASK_PLAN_BLOCKED")
    with drafts._locked(path):
        proposed = next(
            r for r in drafts._rows(drafts._database(path)) if r["environment"] == plan["environment"]
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
            **action_fields,
        }
        _current(path, request, reference)
        # 同一冻结计划永久复用；重新预检生成的新计划是另一次观察，不复用过期证据。
        key = plans._digest(
            {"plan": identifier, "digest": plan["digest"], "target": target["id"], **action_fields}
        )
        task_id = "task_" + key[:32]
        with gateway_db.initialize(drafts._database(path)) as conn:
            drafts.initialize_tasks(conn)
            conn.execute(
                "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,updated_at) "
                "VALUES(?,?,?,'queued',?)",
                (task_id, plan["environment"], json.dumps(request), time.time()),
            )
            return _public(_row(conn, task_id))


def submit(path, identity, identifier, reference):
    return _plan_task(path, identity, identifier, reference, {"action": "site-preflight"})


def submit_verify(path, identity, identifier, check, reference):
    """冻结计划下的受管验收任务；跨主机检查由受管执行端实际取证，不接受表单结论。"""
    if not isinstance(check, str) or check not in VERIFY_CHECKS:
        raise TaskRefused("ENVIRONMENT_TASK_CHECK_INVALID")
    return _plan_task(path, identity, identifier, reference, {"action": "host-verify", "check": check})


def submit_workspace(
    path,
    identity,
    identifier,
    tenant_id,
    reference,
    *,
    knowledge=False,
    connect=False,
    application=False,
    consumer=False,
    consumer_source=None,
    binding="",
    target_binding="",
    candidate_parent=None,
):
    if not isinstance(tenant_id, str) or not drafts.REFERENCE_PATTERN.fullmatch(tenant_id):
        raise TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
    with drafts._locked(path):
        with gateway_db.connection(drafts._database(path), readonly=True) as conn:
            source = _row(conn, identifier)
        request = json.loads(source["request"])
        _authorize(path, identity, source["environment"])
        _current(path, request, reference)
        parent_action = (
            "knowledge-connect"
            if application
            else "knowledge-prepare"
            if connect
            else "workspace-prepare"
            if knowledge
            else "site-preflight"
        )
        if request.get("action", "site-preflight") != parent_action or source["state"] != "observed":
            raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        if knowledge or connect or application:
            if request["tenant_id"] != tenant_id or _public(source)["state"] != "observed":
                raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        else:
            validate_receipt(json.loads(source["result"]), source["environment"])
        proposed = next(
            r for r in drafts._rows(drafts._database(path)) if r["environment"] == source["environment"]
        )
        tenant = next((t for t in json.loads(proposed["body"])["tenants"] if t["id"] == tenant_id), None)
        if tenant is None:
            raise TaskRefused("ENVIRONMENT_TASK_TENANT_INVALID")
        action = (
            "consumer-prepare"
            if consumer
            else "operator-prepare"
            if application
            else "knowledge-connect"
            if connect
            else "knowledge-prepare"
            if knowledge
            else "workspace-prepare"
        )
        request = {
            **request,
            "identity": identity,
            "action": action,
            "tenant_id": tenant_id,
            "tenant_name": tenant["name"],
            "site_task": request["site_task"] if knowledge or connect or application else identifier,
            **(
                {
                    "workspace_task": identifier,
                    "workspace_id": json.loads(source["result"])["workspace_id"],
                    "binding": binding,
                    "target_binding": target_binding,
                }
                if knowledge
                else {}
            ),
            **(
                {
                    "connection_task": identifier,
                    "connection_id": json.loads(source["result"])["connection_id"],
                    "binding": binding,
                    "gateway_url": json.loads(proposed["body"])["gateway_url"],
                }
                if application
                else {}
            ),
            **(
                {
                    "knowledge_task": identifier,
                    "knowledge_base_id": json.loads(source["result"])["knowledge_base_id"],
                    "binding": binding,
                }
                if connect
                else {}
            ),
        }
        request.pop("resources", None)
        request.pop("candidate_parent", None)
        if consumer:
            request["consumer_source"] = consumer_source
        digest = plans._digest(
            {
                "plan": request["plan"]["id"],
                "digest": request["plan"]["digest"],
                "action": action,
                "tenant_id": tenant_id,
                **({"binding": binding} if knowledge or connect or application else {}),
                **({"source": consumer_source} if consumer else {}),
            }
        )
        task_id = "task_" + digest[:32]
        with gateway_db.connection(drafts._database(path), write=True) as conn:
            parent_request = None
            if candidate_parent is not None:
                parent_row = _row(conn, candidate_parent[0])
                parent_request = json.loads(parent_row["request"])
                _authorize(path, tuple(parent_request["identity"]), parent_row["environment"])
                _current(path, parent_request, reference)
                if (
                    parent_request.get("action") != "candidate-prepare"
                    or parent_row["state"] != "waiting"
                    or parent_row["cancel"]
                    or parent_request["bindings"] != candidate_parent[1]
                    or parent_request["plan"]["id"] != request["plan"]["id"]
                    or parent_request["tenant_id"] != tenant_id
                    or parent_request["children"].get(parent_action) != identifier
                ):
                    raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
                request["candidate_parent"] = candidate_parent[0]
            conn.execute(
                "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,updated_at) "
                "VALUES(?,?,?,'queued',?)",
                (task_id, source["environment"], json.dumps(request), time.time()),
            )
            if parent_request is not None:
                parent_request["children"][action] = task_id
                conn.execute(
                    "UPDATE environment_tasks SET request=?,updated_at=? WHERE id=?",
                    (json.dumps(parent_request), time.time(), candidate_parent[0]),
                )
            return _public(_row(conn, task_id))


def workspace_request(path, reference, identifier, owner, *, reconciling=True, action="workspace-prepare"):
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        row = _row(conn, identifier)
        request = json.loads(row["request"])
        if (
            request.get("action") != action
            or row["owner"] != owner
            or row["state"] != "running"
            or row["lease_until"] <= time.time()
        ):
            raise TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        _authorize(path, tuple(request["identity"]), row["environment"])
        _current(path, request, reference, reconciling=reconciling)
        if not reconciling:
            if request.get("candidate_parent"):
                parent = _row(conn, request["candidate_parent"])
                if parent["cancel"] or parent["state"] != "waiting":
                    raise TaskRefused("ENVIRONMENT_TASK_CANCELLED")
            source = _row(conn, request["site_task"])
            source_request = json.loads(source["request"])
            if source["state"] != "observed" or source_request["plan"]["id"] != request["plan"]["id"]:
                raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
            try:
                validate_receipt(json.loads(source["result"]), row["environment"])
            except TaskRefused as exc:
                raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED") from exc
            if action in {"knowledge-prepare", "knowledge-connect", "operator-prepare", "consumer-prepare"}:
                parent = _row(conn, request["workspace_task"])
                if _public(parent)["state"] != "observed":
                    raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
                if (
                    action in {"knowledge-connect", "operator-prepare", "consumer-prepare"}
                    and _public(_row(conn, request["knowledge_task"]))["state"] != "observed"
                ):
                    raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
                if (
                    action in {"operator-prepare", "consumer-prepare"}
                    and _public(_row(conn, request["connection_task"]))["state"] != "observed"
                ):
                    raise TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        if action in {
            "knowledge-prepare",
            "knowledge-connect",
            "operator-prepare",
            "consumer-prepare",
        } and not request.get("resources"):
            previous = conn.execute(
                "SELECT request FROM environment_tasks WHERE environment=? AND id!=?"
                " AND json_extract(request,'$.action')=?"
                " AND json_extract(request,'$.tenant_id')=?"
                " AND json_extract(request,'$.resources') IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (row["environment"], identifier, action, request["tenant_id"]),
            ).fetchone()
            if previous:
                request["resources"] = json.loads(previous["request"])["resources"]
        return {**request, "cancel_requested": bool(row["cancel"])}


def save_knowledge_progress(path, reference, identifier, owner, resources, *, action="knowledge-prepare"):
    request = workspace_request(path, reference, identifier, owner, action=action)
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row = _row(conn, identifier)
        if row["owner"] != owner or row["state"] != "running" or row["lease_until"] <= time.time():
            raise TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        stored = json.loads(row["request"])
        beginning = resources.get("knowledge_create", {}).get("attempted") and not stored.get(
            "resources", {}
        ).get("knowledge_create", {}).get("attempted")
        if action in {"knowledge-connect", "operator-prepare", "consumer-prepare"}:
            beginning = resources.get("attempted") and not stored.get("resources", {}).get("attempted")
        if beginning:
            _authorize(path, tuple(stored["identity"]), row["environment"])
            _current(path, stored, reference)
            if row["cancel"]:
                raise TaskRefused("ENVIRONMENT_TASK_CANCELLED")
        conn.execute(
            "UPDATE environment_tasks SET request=? WHERE id=?",
            (
                json.dumps(
                    {k: v for k, v in {**request, "resources": resources}.items() if k != "cancel_requested"}
                ),
                identifier,
            ),
        )


def finish_workspace(path, reference, identifier, owner, result):
    return worker(
        path, reference, {"id": identifier, "owner": owner, "result": result}, internal_workspace=True
    )


def inspect(path, identity, *, identifier=None, environment=None, cancel=False, reference=None):
    if environment is not None and (
        not isinstance(environment, str) or not drafts.REFERENCE_PATTERN.fullmatch(environment)
    ):
        raise TaskRefused("ENVIRONMENT_TASK_INVALID")
    with drafts._locked(path):
        if environment is not None:
            _authorize(path, identity, environment)
        database = drafts._database(path)
        if not database.exists():
            if environment is not None:
                return {"tasks": []}
            raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")

        def current_public(row):
            result = _public(row)
            if result["state"] in {"observed", "blocked"}:
                try:
                    _current(path, json.loads(row["request"]), reference, reconciling=True)
                except (ValueError, OSError, RuntimeError):
                    result["state"] = "expired"
            return result

        with gateway_db.connection(database, write=cancel, readonly=not cancel) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
                if environment is not None:
                    return {"tasks": []}
                raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
            if environment is not None:
                return {
                    "tasks": [
                        current_public(dict(r))
                        for r in conn.execute(
                            "SELECT * FROM environment_tasks WHERE environment=? "
                            "ORDER BY updated_at DESC LIMIT 20",
                            (environment,),
                        )
                    ]
                }
            row = _row(conn, identifier)
            _authorize(path, identity, row["environment"])
            if cancel:
                if json.loads(row["request"]).get("action") == "candidate-prepare":
                    for child_id in json.loads(row["request"])["children"].values():
                        child = _row(conn, child_id)
                        if json.loads(child["request"]).get("candidate_parent") == identifier and child[
                            "state"
                        ] in {"queued", "running", "reconcile"}:
                            child_state = (
                                child["state"] if child["state"] in {"running", "reconcile"} else "cancelled"
                            )
                            conn.execute(
                                "UPDATE environment_tasks SET cancel=1,state=?,updated_at=? WHERE id=?",
                                (child_state, time.time(), child_id),
                            )
                state = (
                    row["state"]
                    if row["state"] in {"running", "reconcile", "executing", "checking"}
                    else "cancelled"
                )
                conn.execute(
                    "UPDATE environment_tasks SET cancel=1,state=?,updated_at=? WHERE id=?",
                    (state, time.time(), identifier),
                )
                row = _row(conn, identifier)
            return current_public(row)


def validate_receipt(value, environment):
    fields = {
        "id",
        "scope",
        "environment",
        "version",
        "source_revision",
        "artifact_sha256",
        "config_sha256",
        "registry_sha256",
        "checks",
        "state",
        "executable",
        "created_at",
        "expires_at",
        "resources",
    }
    checks = {
        "local-path-identity",
        "installed-artifact",
        "configuration",
        "registry",
        "recovery",
        "service-readiness",
        "schema-inflight",
        "resource-headroom",
        "managed-host-identity",
        "formal-network",
        "business",
    }
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"runtime_sha256"}):
        raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
    if "runtime_sha256" in value and (
        not isinstance(value["runtime_sha256"], str)
        or value["runtime_sha256"] != "absent"
        and not re.fullmatch(r"[0-9a-f]{64}", value["runtime_sha256"])
    ):
        raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
    valid = (
        value["environment"] == environment
        and value["scope"] == "installed-instance-readiness-only"
        and value["executable"] is False
        and isinstance(value["state"], str)
        and value["state"] in {"observed", "blocked"}
        and isinstance(value["id"], str)
        and re.fullmatch(r"site_[0-9a-f]{32}", value["id"])
        and isinstance(value["version"], str)
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[0-9a-f]{12})?", value["version"])
        and isinstance(value["source_revision"], str)
        and re.fullmatch(r"[0-9a-f]{40}", value["source_revision"])
        and all(
            isinstance(value[k], str) and re.fullmatch(r"[0-9a-f]{64}", value[k])
            for k in ("artifact_sha256", "config_sha256")
        )
        and isinstance(value["registry_sha256"], str)
        and (value["registry_sha256"] == "absent" or re.fullmatch(r"[0-9a-f]{64}", value["registry_sha256"]))
        and all(
            type(value[k]) in (int, float) and math.isfinite(value[k]) for k in ("created_at", "expires_at")
        )
        and value["expires_at"] - value["created_at"] == 900
        and value["created_at"] <= time.time() + 30
        and value["expires_at"] > time.time()
        and isinstance(value["checks"], list)
        and len(value["checks"]) == len(checks)
        and all(
            isinstance(c, dict)
            and set(c) == {"id", "state"}
            and isinstance(c["id"], str)
            and isinstance(c["state"], str)
            for c in value["checks"]
        )
        and {c["id"] for c in value["checks"]} == checks
        and all(
            c["state"] == "pending"
            if c["id"] in {"managed-host-identity", "formal-network", "business"}
            else c["state"] in {"passed", "blocked"}
            for c in value["checks"]
        )
        and value["state"]
        == ("blocked" if any(c["state"] == "blocked" for c in value["checks"]) else "observed")
        and isinstance(value["resources"], dict)
        and set(value["resources"]) == {"available_memory_bytes", "available_disk_bytes"}
        and all(type(n) is int and n >= 0 for n in value["resources"].values())
    )
    if not valid:
        raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")


def validate_verify(result, environment):
    """受管主机验收回执：只收执行端现场取证的结论形状，不代替网络/业务验收。"""
    evidence = result.get("evidence") if isinstance(result, dict) else None
    valid = (
        isinstance(result, dict)
        and set(result)
        == {
            "id",
            "scope",
            "environment",
            "check",
            "state",
            "executable",
            "evidence",
            "created_at",
            "expires_at",
        }
        and result["scope"] == "managed-host-verification-only"
        and result["check"] == "managed-host-identity"
        and result["environment"] == environment
        and result["executable"] is False
        and isinstance(result["id"], str)
        and re.fullmatch(r"verify_[0-9a-f]{32}", result["id"])
        and isinstance(result["state"], str)
        and result["state"] in {"observed", "blocked"}
        and all(
            type(result[k]) in (int, float) and math.isfinite(result[k]) for k in ("created_at", "expires_at")
        )
        and result["expires_at"] - result["created_at"] == 900
        and result["created_at"] <= time.time() + 30
        and result["expires_at"] > time.time()
        and isinstance(evidence, dict)
        and set(evidence)
        == {
            "declared_target",
            "resolved_host",
            "remote_hostname",
            "machine_id_sha256",
            "service_user_present",
        }
        and isinstance(evidence["declared_target"], str)
        and drafts.REFERENCE_PATTERN.fullmatch(evidence["declared_target"])
        and isinstance(evidence["resolved_host"], str)
        and (evidence["resolved_host"] == "" or drafts.REFERENCE_PATTERN.fullmatch(evidence["resolved_host"]))
        and isinstance(evidence["remote_hostname"], str)
        and len(evidence["remote_hostname"]) <= 253
        and all(
            not re.fullmatch(r"[0-9a-f]{32}", evidence[key])
            for key in ("declared_target", "resolved_host", "remote_hostname")
        )
        and isinstance(evidence["machine_id_sha256"], str)
        and (
            evidence["machine_id_sha256"] == ""
            or re.fullmatch(r"[0-9a-f]{64}", evidence["machine_id_sha256"])
        )
        and type(evidence["service_user_present"]) is bool
        and (result["state"] == "observed")
        == (
            bool(evidence["remote_hostname"])
            and bool(re.fullmatch(r"[0-9a-f]{64}", evidence["machine_id_sha256"]))
            and evidence["service_user_present"]
        )
    )
    if not valid:
        raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")


def worker(path, reference, payload, *, internal_workspace=False):
    if set(payload) not in ({"owner"}, {"id", "owner"}, {"id", "owner", "result"}, {"id", "owner", "error"}):
        raise TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    owner = payload["owner"]
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    with drafts._locked(path):
        if set(payload) == {"owner"}:
            selected = _next(path, reference)
            if selected is None:
                return {"claimed": False, "task": None}
            payload = {**payload, "id": selected}
        with gateway_db.connection(drafts._database(path), readonly=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
                raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
            row = _row(conn, payload["id"])
        request = json.loads(row["request"])
        if request.get("action") in {"candidate-prepare", "candidate-install", "candidate-register"}:
            raise TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
        _authorize(path, tuple(request["identity"]), row["environment"])
        if (
            set(payload) == {"id", "owner"}
            and row["state"] in {"queued", "running", "reconcile"}
            and (not row["cancel"] or row["state"] in {"running", "reconcile"})
        ):
            _current(path, request, reference, reconciling=row["state"] in {"running", "reconcile"})
        drift = False
        if "result" in payload or "error" in payload:
            try:
                _current(path, request, reference, reconciling=True)
            except (ValueError, OSError, RuntimeError):
                drift = True
        result = _worker_write(path, payload, row, request, owner, drift, internal_workspace)
        if result.get("claimed") is False and result["task"]["state"] in {"observed", "blocked"}:
            try:
                _current(path, request, reference, reconciling=True)
            except (ValueError, OSError, RuntimeError):
                result["task"]["state"] = "expired"
        return result


def _next(path, reference):
    database = drafts._database(path)
    if not database.exists():
        return None
    now = time.time()
    with gateway_db.connection(database, readonly=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
            return None
        candidates = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM environment_tasks AS task WHERE state IN ('queued','running','reconcile') "
                "AND json_extract(request,'$.action') IS NOT 'candidate-prepare' "
                "AND json_extract(request,'$.action') IS NOT 'candidate-install' "
                "AND json_extract(request,'$.action') IS NOT 'candidate-register' "
                "AND lease_until<=? AND (state!='reconcile' OR updated_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM environment_tasks AS live "
                "WHERE live.environment=task.environment AND live.id!=task.id "
                "AND live.state IN ('running','executing') AND live.lease_until>?) "
                "ORDER BY updated_at LIMIT 64",
                (now, now - 60, now),
            )
        ]
    for row in candidates:
        request = json.loads(row["request"])
        try:
            _authorize(path, tuple(request["identity"]), row["environment"])
            _current(path, request, reference, reconciling=row["state"] != "queued")
            with gateway_db.connection(database, readonly=True) as conn:
                busy = conn.execute(
                    "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
                    "AND state IN ('running','executing') AND lease_until>? LIMIT 1",
                    (row["environment"], row["id"], now),
                ).fetchone()
            if not busy:
                return row["id"]
        except (ValueError, OSError, RuntimeError):
            # 失效队首须留下具名结论，不重复执行或饿死另一个环境。
            with gateway_db.connection(database, write=True) as conn:
                conn.execute(
                    "UPDATE environment_tasks SET state='expired',error='ENVIRONMENT_TASK_PLAN_EXPIRED',"
                    "owner='',lease_until=0,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
    return None


def _worker_write(path, payload, row, request, owner, drift, internal_workspace):
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        now = time.time()
        if set(payload) == {"id", "owner"}:
            if row["state"] in FINISHED_STATES:
                return {"claimed": False, "task": _public(row)}
            if row["lease_until"] > now:
                return {"claimed": False, "task": _public(row)}
            if conn.execute(
                "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
                "AND state IN ('running','executing') AND lease_until>? LIMIT 1",
                (row["environment"], row["id"], now),
            ).fetchone():
                raise TaskRefused("ENVIRONMENT_TASK_BUSY")
            conn.execute(
                "UPDATE environment_tasks SET state='running',owner=?,lease_until=?,updated_at=? WHERE id=?",
                (owner, now + 300, now, row["id"]),
            )
            return {
                "claimed": True,
                "id": row["id"],
                "action": request.get("action", "site-preflight"),
                "check": request.get("check"),
                "environment": row["environment"],
                "target": request["target"],
                "installer": request["installer"],
            }
        if row["owner"] != owner or row["state"] != "running" or row["lease_until"] <= now:
            raise TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        result, state, error = {}, "reconcile", "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"
        if "result" in payload:
            result = payload["result"]
            if request.get("action", "site-preflight") in {
                "workspace-prepare",
                "knowledge-prepare",
                "knowledge-connect",
                "operator-prepare",
                "consumer-prepare",
            }:
                if not internal_workspace:
                    raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
                if (
                    not isinstance(result, dict)
                    or result.get("state") not in {"observed", "blocked"}
                    or result.get("scope")
                    != {
                        "knowledge-connect": "candidate-knowledge-connection-only",
                        "operator-prepare": "candidate-operator-application-only",
                        "consumer-prepare": "candidate-consumer-application-only",
                        "knowledge-prepare": "candidate-knowledge-only",
                        "workspace-prepare": "candidate-workspace-only",
                    }[request["action"]]
                    or result.get("environment") != row["environment"]
                    or result.get("tenant_id") != request["tenant_id"]
                    or result.get("executable") is not False
                ):
                    raise TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            elif request.get("action") == "host-verify":
                validate_verify(result, row["environment"])
            else:
                validate_receipt(result, row["environment"])
            state, error = result["state"], result.get("error_code", "")
        elif payload["error"] != "ENVIRONMENT_TASK_TARGET_UNCONFIRMED":
            raise TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
        if drift:
            state, error = "expired", "ENVIRONMENT_TASK_PLAN_EXPIRED"
        if row["cancel"] and result:
            state = "cancelled"
        conn.execute(
            "UPDATE environment_tasks SET state=?,owner='',lease_until=0,result=?,error=?,"
            "updated_at=? WHERE id=?",
            (state, json.dumps(result), error, now, row["id"]),
        )
        return _public(_row(conn, row["id"]))


def active_connection(path, reference, scope):
    environment, tenant, workspace, library = scope
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM environment_tasks WHERE environment=?"
            " AND json_extract(request,'$.action')='knowledge-connect'"
            " AND json_extract(request,'$.tenant_id')=? ORDER BY rowid DESC LIMIT 1",
            (environment, tenant),
        ).fetchone()
        if row is None or row["state"] != "observed" or row["cancel"]:
            raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
        request = json.loads(row["request"])
        if request["workspace_id"] != workspace or request["knowledge_base_id"] != library:
            raise TaskRefused("ENVIRONMENT_TASK_UNAVAILABLE")
        _authorize(path, tuple(request["identity"]), environment)
        _current(path, request, reference, reconciling=True)
        return request
