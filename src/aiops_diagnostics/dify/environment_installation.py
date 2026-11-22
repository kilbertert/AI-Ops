"""停用候选的持久安装协调；副作用和恢复仍归原固定安装器。"""

import hashlib
import json
import re
import time

from aiops_diagnostics.common.private_files import validate_private_directory
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.environment_artifacts import candidate_files, runtime_files

from . import environment_candidate_preparation as preparation
from . import environment_management as management
from . import environment_runtime as runtime

ACTION = "candidate-install"
COLD_ACTION = "site-install"


def binding(settings, target):
    body, _ = runtime._private_json(settings.environment_installation_file)
    if body.get("schema") != "aiops-environment-installation/1" or set(body) != {"schema", "targets"}:
        raise ValueError("installation references invalid")
    rows = body["targets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("installation targets invalid")
    if any(
        not isinstance(r, dict) or set(r) not in ({"id", "node_file"}, {"id", "node_file", "capacity"})
        for r in rows
    ):
        raise ValueError("installation target invalid")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate installation targets")
    matches = [r for r in rows if r["id"] == target]
    if len(matches) != 1:
        raise tasks.TaskRefused("ENVIRONMENT_INSTALLATION_CONFIG_REQUIRED")
    node_file = matches[0]["node_file"]
    node, node_sha = runtime._private_json(node_file)
    if not isinstance(node, dict) or set(node) != {
        "environment",
        "config_file",
        "manifest_file",
        "proxy_base_url",
    }:
        raise ValueError("installation node invalid")
    _, config_sha = runtime._private_json(node["config_file"])
    _, manifest_sha = runtime._private_json(node["manifest_file"])
    return node_file, plans._digest([node_sha, config_sha, manifest_sha])


def current(context, conn, identifier, *, fresh=False):
    settings = context.settings
    path, reference = management._path(context), settings.environment_onboarding_file
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    tasks._current(path, request, reference, reconciling=not fresh)
    parent = tasks._row(conn, request["preparation"])
    parent_request = json.loads(parent["request"])
    if (
        parent["cancel"]
        or parent["state"] != "observed"
        or parent_request["bindings"] != preparation._bindings(settings, parent_request["consumer_source"])
        or binding(settings, request["target"]["id"])[1] != request["node_binding"]
        or fresh
        and (row["cancel"] or parent_request["plan"]["expires_at"] <= time.time())
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
    return row, request


def submit_cold(conn, environment, identity, parent_id, request):
    """真空目标的初装子任务；与安装面互斥，目标与制品引用随父冻结。"""
    task_id = (
        "task_"
        + plans._digest({"action": COLD_ACTION, "preparation": parent_id, "tenant": request["tenant_id"]})[
            :32
        ]
    )
    if conn.execute(
        "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
        "AND state IN ('waiting','executing','checking') "
        "AND json_extract(request,'$.action') IN "
        "('site-install','candidate-install','candidate-register','candidate-validate',"
        "'environment-publish') LIMIT 1",
        (environment, task_id),
    ).fetchone():
        raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
    conn.execute(
        "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
        "VALUES(?,?,?,'waiting',?,?)",
        (
            task_id,
            environment,
            json.dumps(
                {
                    "identity": list(identity),
                    "action": COLD_ACTION,
                    "preparation": parent_id,
                    "tenant_id": request["tenant_id"],
                    "plan": request["plan"],
                    "target": request["target"],
                    "installer": request["installer"],
                    "attempted": False,
                }
            ),
            json.dumps(
                {
                    "scope": "site-cold-installation-only",
                    "expires_at": request["plan"]["expires_at"],
                    "installation": "not_executed",
                }
            ),
            time.time(),
        ),
    )
    return task_id


def current_cold(context, conn, identifier, *, fresh=False):
    """初装任务门禁：父任务仍在等待首步、包含初装已声明，取消即拒。"""
    settings = context.settings
    path, reference = management._path(context), settings.environment_onboarding_file
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    if request.get("action") != COLD_ACTION:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    # 已派发请求只用冻结输入回收真实结果；撤权、取消或父终态不能再把它变成拒绝。
    if not fresh and request.get("dispatched"):
        return row, request
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    tasks._current(path, request, reference, reconciling=not fresh)
    parent = tasks._row(conn, request["preparation"])
    parent_request = json.loads(parent["request"])
    if (
        parent["cancel"]
        or row["cancel"]
        or parent["state"] != "waiting"
        or parent_request.get("action") != preparation.ACTION
        or parent_request.get("initial_installation") is not True
        or parent_request["children"].get(COLD_ACTION) != row["id"]
        or parent_request["bindings"] != preparation._bindings(settings, parent_request["consumer_source"])
        or fresh
        and parent_request["plan"]["expires_at"] <= time.time()
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
    return row, request


def submit(context, identity, identifier):
    settings, path = context.settings, management._path(context)
    if not settings.environment_installation_file:
        raise tasks.TaskRefused("ENVIRONMENT_INSTALLATION_CONFIG_REQUIRED")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        parent = tasks._row(conn, identifier)
        request = json.loads(parent["request"])
        tasks._authorize(path, identity, parent["environment"])
        tasks._current(path, request, settings.environment_onboarding_file)
        if (
            request.get("action") != preparation.ACTION
            or parent["cancel"]
            or tasks._public(parent)["state"] != "observed"
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        node_file, node_binding = binding(settings, request["target"]["id"])
        site = tasks._row(conn, request["children"]["site-preflight"])
        observed = json.loads(site["result"])
        if site["state"] != "observed" or observed["state"] != "observed":
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        expected = {k: observed[k] for k in ("artifact_sha256", "config_sha256", "registry_sha256")}
        # 旧现场回执没有运行摘要；只允许显式新回执，不猜现场是否已有运行工件。
        if "runtime_sha256" not in observed:
            raise tasks.TaskRefused("ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED")
        expected["runtime_sha256"] = observed["runtime_sha256"]
        task_id = "task_" + plans._digest([ACTION, identifier, expected, node_binding])[:32]
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
            "AND state IN ('waiting','executing','checking') "
            "AND json_extract(request,'$.action') "
            "IN ('site-install','candidate-install','candidate-register','candidate-validate',"
            "'environment-publish')",
            (parent["environment"], task_id),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        request = {
            **request,
            "action": ACTION,
            "identity": identity,
            "preparation": identifier,
            "node_file": node_file,
            "node_binding": node_binding,
            "expected": expected,
            "attempted": False,
        }
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (
                task_id,
                parent["environment"],
                json.dumps(request),
                json.dumps(
                    {
                        "scope": "candidate-installation-only",
                        "expires_at": request["plan"]["expires_at"],
                        "installation": "not_executed",
                        "registration": "not_executed",
                        "business": "unverified",
                        "activation": "not_executed",
                    }
                ),
                time.time(),
            ),
        )
        current(context, conn, task_id, fresh=True)
        return tasks._public(tasks._row(conn, task_id))


def dispatch(context, payload):
    path = management._path(context)
    if payload.get("installation_claim") is True and set(payload) == {"installation_claim", "owner"}:
        owner = payload["owner"]
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
        if not drafts._database(path).exists():
            return {"claimed": False, "task": None}
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
                return {"claimed": False, "task": None}
            rows = conn.execute(
                "SELECT id FROM environment_tasks WHERE json_extract(request,'$.action') "
                "IN ('site-install','candidate-install','candidate-register','candidate-validate',"
                "'environment-publish') "
                "AND state IN ('waiting','executing','checking') AND lease_until<=? "
                # 与现场队列reconcile同规：checking观察间隔60秒，残迹任务不抢占认领面；
                # 已取消的在飞任务保持立即可领，排空到终态不等待。
                "AND (state!='checking' OR updated_at<=? OR cancel=1) "
                "AND NOT EXISTS(SELECT 1 FROM environment_tasks live "
                "WHERE live.environment=environment_tasks.environment AND live.id!=environment_tasks.id "
                "AND live.state IN ('running','executing') AND live.lease_until>?) "
                "ORDER BY updated_at LIMIT 20",
                (time.time(), time.time() - 60, time.time()),
            ).fetchall()
            for selected in rows:
                row = tasks._row(conn, selected[0])
                request = json.loads(row["request"])
                try:
                    if request["action"] == "environment-publish":
                        from .environment_publication import current as publication_current

                        publication_current(context, conn, row["id"], fresh=not request["attempted"])
                    elif request["action"] == "candidate-validate":
                        from .environment_validation import current as validation_current

                        validation_current(context, conn, row["id"], fresh=not request["attempted"])
                    elif request["action"] == "candidate-register":
                        from .environment_registration import current as registration_current

                        registration_current(context, conn, row["id"], fresh=not request["attempted"])
                    elif request["action"] == COLD_ACTION:
                        current_cold(context, conn, row["id"], fresh=not request["attempted"])
                    else:
                        current(context, conn, row["id"], fresh=not request["attempted"])
                except (ValueError, RuntimeError, OSError, KeyError, TypeError):
                    conn.execute(
                        "UPDATE environment_tasks SET state='blocked',error=?,updated_at=? WHERE id=?",
                        ("ENVIRONMENT_TASK_OBSERVATION_REQUIRED", time.time(), row["id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE environment_tasks SET state='executing',owner=?,lease_until=?,updated_at=? "
                    "WHERE id=?",
                    (owner, time.time() + 900, time.time(), row["id"]),
                )
                return {"claimed": True, "id": row["id"], "action": request["action"]}
            return {"claimed": False, "task": None}
    if payload.get("installation") not in {"stage", "authorize", "complete"} or set(payload) not in (
        {"installation", "id", "owner"},
        {"installation", "id", "owner", "result"},
    ):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row = tasks._row(conn, payload["id"])
        request = json.loads(row["request"])
        if request.get("action") not in {ACTION, COLD_ACTION}:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
        if (
            row["state"] != "executing"
            or row["owner"] != payload["owner"]
            or row["lease_until"] <= time.time()
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        if payload["installation"] == "authorize":
            (current_cold if request["action"] == COLD_ACTION else current)(
                context, conn, payload["id"], fresh=True
            )
            if not request["attempted"] or request.get("dispatched"):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
            request["dispatched"] = True
            conn.execute(
                "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"])
            )
            return {"authorized": True}
        if payload["installation"] == "complete":
            result = payload.get("result")
            if request["action"] == COLD_ACTION:
                # 已装/初装成功才算observed；外来占机只信installed.json账本形状。
                installed_fields = ("artifact_sha256", "config_sha256", "registry_sha256")
                if (
                    not isinstance(result, dict)
                    or result.get("id") != row["id"]
                    or result.get("environment") != row["environment"]
                    or result.get("scope") != "site-cold-installation-only"
                    or result.get("executable") is not False
                    or result.get("state") not in {"observed", "blocked", "reconcile"}
                    or set(result)
                    - {
                        "id",
                        "environment",
                        "scope",
                        "state",
                        "installation",
                        "executable",
                        "error_code",
                        "version",
                        "source_revision",
                        *installed_fields,
                    }
                    or result["state"] == "observed"
                    and (
                        result.get("installation") not in {"installed", "already_installed"}
                        or not isinstance(result.get("version"), str)
                        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[a-f0-9]{12})?", result["version"])
                        or not isinstance(result.get("source_revision"), str)
                        or not re.fullmatch(r"[0-9a-f]{40}", result["source_revision"])
                        or any(
                            not isinstance(result.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", result[k])
                            for k in installed_fields[:2]
                        )
                        or (
                            not isinstance(result.get("registry_sha256"), str)
                            or (
                                result["registry_sha256"] != "absent"
                                and not re.fullmatch(r"[a-f0-9]{64}", result["registry_sha256"])
                            )
                            # 无摘要只属旧config-only账本；本任务新装仍要强摘要。
                            or (
                                result["registry_sha256"] == "absent"
                                and result["installation"] != "already_installed"
                            )
                        )
                        or result["installation"] == "installed"
                        and (
                            result["artifact_sha256"]
                            != request.get("archives", {}).get("application", {}).get("sha256")
                            or result["source_revision"]
                            != request.get("archives", {}).get("application", {}).get("revision")
                        )
                    )
                ):
                    raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            elif (
                not isinstance(result, dict)
                or result.get("id") != row["id"]
                or result.get("environment") != row["environment"]
                or result.get("scope") != "candidate-installation-only"
                or result.get("executable") is not False
                or result.get("state") not in {"observed", "blocked", "reconcile"}
                or result.get("activation") != "not_executed"
                or result.get("registration") != "not_executed"
                or result.get("business") != "unverified"
                or set(result)
                - {
                    "id",
                    "environment",
                    "scope",
                    "state",
                    "installation",
                    "executable",
                    "registration",
                    "activation",
                    "business",
                    "error_code",
                    "version",
                    "source_revision",
                    "artifact_sha256",
                    "config_sha256",
                    "registry_sha256",
                    "runtime_sha256",
                    "source_runtime_sha256",
                    "previous_runtime_sha256",
                }
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            if (
                request["action"] != COLD_ACTION
                and result["state"] == "observed"
                and (
                    not isinstance(result.get("version"), str)
                    or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[a-f0-9]{12})?", result["version"])
                    or result.get("installation") not in {"installed", "unchanged"}
                    or result.get("artifact_sha256") != request["archives"]["application"]["sha256"]
                    or result.get("source_runtime_sha256", result.get("runtime_sha256"))
                    != request["archives"]["runtime"]["sha256"]
                    or not isinstance(result.get("runtime_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", result["runtime_sha256"])
                    or (
                        result.get("runtime_sha256") != request["archives"]["runtime"]["sha256"]
                        and (
                            result.get("previous_runtime_sha256") != request["expected"]["runtime_sha256"]
                            or result.get("source_runtime_sha256") != request["archives"]["runtime"]["sha256"]
                        )
                    )
                    or result.get("source_revision") != request["archives"]["application"]["revision"]
                    or any(
                        not isinstance(result.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", result[k])
                        for k in ("config_sha256", "registry_sha256")
                    )
                )
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
            state = (
                "cancelled"
                if row["cancel"] and result["state"] != "reconcile"
                else "checking"
                if result["state"] == "reconcile"
                else result["state"]
            )
            conn.execute(
                "UPDATE environment_tasks SET state=?,result=?,owner='',lease_until=0,error=?,updated_at=? "
                "WHERE id=?",
                (
                    state,
                    json.dumps({**result, "expires_at": request["plan"]["expires_at"]}),
                    result.get("error_code", ""),
                    time.time(),
                    row["id"],
                ),
            )
            return tasks._public(tasks._row(conn, row["id"]))
        (current_cold if request["action"] == COLD_ACTION else current)(
            context, conn, payload["id"], fresh=not request["attempted"]
        )
        attempted = request["attempted"]
    if not attempted:
        # HTTP/工件导出不占SQLite写事务；新写入前再次核冻结输入与取消。
        settings = context.settings
        directory = settings.data_home / "installation-tasks" / row["id"]
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        validate_private_directory(directory)
        cold = request["action"] == COLD_ACTION
        files = {
            k: directory / (k + ".tar.gz") for k in (("candidate",) if cold else ("candidate", "runtime"))
        }
        if not files["candidate"].exists():
            plans.export(
                path,
                tuple(request["identity"]),
                request["plan"]["id"],
                settings.environment_onboarding_file,
                files["candidate"],
            )
        if not cold:
            parent = request["children"]
            runtime_payload = {
                "identity": list(request["identity"]),
                "plan_id": request["plan"]["id"],
                "tenant_id": request["tenant_id"],
                "operator_task": parent["operator-prepare"],
                "consumer_task": parent["consumer-prepare"],
            }
            if not files["runtime"].exists():
                runtime.export(settings, runtime_payload, request["node_file"], files["runtime"])
            # 原运行导出与模板指纹再次复核，已存在文件不得被重写。
            rendered, _ = runtime.render(settings, runtime_payload, request["node_file"])
            runtime_sha = hashlib.file_digest(files["runtime"].open("rb"), "sha256").hexdigest()
            stored, _ = runtime_files(files["runtime"], runtime_sha, row["environment"])
            if stored != rendered:
                raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        candidate_sha = hashlib.file_digest(files["candidate"].open("rb"), "sha256").hexdigest()
        _, candidate = candidate_files(files["candidate"], candidate_sha, row["environment"])
        if (
            candidate["plan_id"] != request["plan"]["id"]
            or candidate["plan_digest"] != request["plan"]["digest"]
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        proposed = next(
            r for r in drafts._rows(drafts._database(path)) if r["environment"] == row["environment"]
        )
        target = next(
            t
            for t in plans._references(settings.environment_onboarding_file)
            if t["id"] == json.loads(proposed["body"])["deployment_target"]
        )
        archives = {
            "application": target["application"],
            **{
                k: {"path": str(v), "sha256": hashlib.file_digest(v.open("rb"), "sha256").hexdigest()}
                for k, v in files.items()
            },
        }
        with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
            (current_cold if request["action"] == COLD_ACTION else current)(
                context, conn, row["id"], fresh=True
            )
            request["archives"] = archives
            request["attempted"] = True
            conn.execute(
                "UPDATE environment_tasks SET request=? WHERE id=?", (json.dumps(request), row["id"])
            )
    return {
        "id": row["id"],
        "environment": row["environment"],
        "target": request["target"],
        "installer": request["installer"],
        "python": next(
            t["python"]
            for t in plans._references(context.settings.environment_onboarding_file)
            if t["id"] == request["target"]["id"]
        ),
        "expires_at": request["plan"]["expires_at"],
        **({} if request["action"] == COLD_ACTION else {"expected": request["expected"]}),
        "archives": request["archives"],
        "observe_only": attempted,
    }
