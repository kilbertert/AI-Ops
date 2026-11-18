"""冻结现场预检任务：持久去重、授权、租约与结果核对；不安装或激活。"""

import json
import math
import re
import time

from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import gateway_db


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
        "action": "site-preflight",
        "plan_id": request["plan"]["id"],
        "state": state,
        "executable": False,
        "cancel_requested": bool(row["cancel"]),
        "result": result,
        "error_code": row["error"],
        "updated_at": row["updated_at"],
    }


def submit(path, identity, identifier, reference):
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
        }
        _current(path, request, reference)
        # 同一冻结计划永久复用；重新预检生成的新计划是另一次观察，不复用过期证据。
        key = plans._digest(
            {"plan": identifier, "digest": plan["digest"], "target": target["id"], "action": "site-preflight"}
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
                state = row["state"] if row["state"] in {"running", "reconcile"} else "cancelled"
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
    if not isinstance(value, dict) or set(value) != fields:
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


def worker(path, reference, payload):
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
        result = _worker_write(path, payload, row, request, owner, drift)
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
                "SELECT * FROM environment_tasks WHERE state IN ('queued','running','reconcile') "
                "AND lease_until<=? AND (state!='reconcile' OR updated_at<=?) ORDER BY updated_at LIMIT 64",
                (now, now - 60),
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
                    "AND state='running' AND lease_until>? LIMIT 1",
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


def _worker_write(path, payload, row, request, owner, drift):
    with gateway_db.connection(drafts._database(path), write=True) as conn:
        now = time.time()
        if set(payload) == {"id", "owner"}:
            if row["state"] in {"observed", "blocked", "cancelled", "expired"}:
                return {"claimed": False, "task": _public(row)}
            if row["lease_until"] > now:
                return {"claimed": False, "task": _public(row)}
            if conn.execute(
                "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
                "AND state='running' AND lease_until>? LIMIT 1",
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
                "environment": row["environment"],
                "target": request["target"],
                "installer": request["installer"],
            }
        if row["owner"] != owner or row["state"] != "running" or row["lease_until"] <= now:
            raise TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        result, state, error = {}, "reconcile", "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"
        if "result" in payload:
            result = payload["result"]
            validate_receipt(result, row["environment"])
            state, error = result["state"], ""
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
