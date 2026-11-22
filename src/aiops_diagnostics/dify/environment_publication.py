"""环境正式发布协调；沿原任务账本保存意图与逐端结果。"""

import hashlib
import json
import time
from pathlib import Path

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db

from . import environment_management as management
from . import environment_validation as validation

ACTION = "environment-publish"


def submit(context, identity, payload):
    if (
        set(payload)
        != {
            "validation_task",
            "publication_plan",
            "confirm_readonly",
            "confirm_customer_knowledge",
            "acceptance_ref",
        }
        or payload["confirm_readonly"] is not True
        or payload["confirm_customer_knowledge"] is not True
    ):
        raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_CONFIRM_REQUIRED")
    reference = payload["acceptance_ref"]
    if reference != "readonly-diagnosis":
        raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_CONFIRM_REQUIRED")
    path = management._path(context)
    identifier = (
        "task_"
        + plans._digest([ACTION, payload["validation_task"], payload["publication_plan"], reference])[:32]
    )
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        existing = conn.execute("SELECT id FROM environment_tasks WHERE id=?", (identifier,)).fetchone()
        if existing:
            row = tasks._row(conn, identifier)
            tasks._authorize(path, identity, row["environment"])
            return tasks._public(row)
    plan = plans.read(
        path, identity, payload["publication_plan"], context.settings.environment_onboarding_file
    )
    if plan["state"] == "expired":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        source, original = validation.current(context, conn, payload["validation_task"])
        result = json.loads(source["result"])
        if (
            source["state"] != "observed"
            or source["cancel"]
            or original["plan"]["digest"] != plan["digest"]
            or original["capabilities"] != ["consumer", "offline", "order"]
            or any(c["state"] != "passed" for c in result["checks"])
        ):
            raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_VALIDATION_REQUIRED")
        for check in result["checks"]:
            from aiops_diagnostics.gateway.environment_business import validate_result

            validate_result(check, check["capability"])
        identifier = "task_" + plans._digest([ACTION, source["id"], plan["id"], reference])[:32]
        if conn.execute(
            "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
            "AND state IN ('waiting','executing','checking') AND json_extract(request,'$.action') "
            "IN ('site-install','candidate-install','candidate-register',"
            "'candidate-validate','environment-publish')",
            (source["environment"], identifier),
        ).fetchone():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        request = {
            **original,
            "action": ACTION,
            "identity": identity,
            "plan": plan,
            "validation_task": source["id"],
            "acceptance_ref": reference,
            "attempted": False,
            "steps": {},
            "validation": result,
            "phase": "readiness",
            "references_sha256": hashlib.sha256(
                Path(context.settings.environment_onboarding_file).read_bytes()
            ).hexdigest(),
        }
        conn.execute(
            "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'waiting',?,?)",
            (
                identifier,
                source["environment"],
                json.dumps(request),
                json.dumps(
                    {
                        "scope": "environment-publication",
                        "expires_at": plan["expires_at"],
                        "activated": False,
                        "phase": "readiness",
                        "business_accuracy": "unconfirmed",
                    }
                ),
                time.time(),
            ),
        )
        return tasks._public(tasks._row(conn, identifier))


def current(context, conn, identifier, *, fresh=False):
    row = tasks._row(conn, identifier)
    request = json.loads(row["request"])
    if request.get("action") != ACTION:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    path = management._path(context)
    tasks._authorize(path, tuple(request["identity"]), row["environment"])
    if not request.get("attempted"):
        validation.current(context, conn, request["validation_task"], fresh=fresh)
        tasks._current(path, request, context.settings.environment_onboarding_file, reconciling=not fresh)
    if fresh and request.get("attempted"):
        source_record = (
            Path(context.settings.data_home) / "publication-bundles" / row["id"] / "source-operation.json"
        )
        if not request.get("source_updated") and not source_record.exists():
            tasks._current(path, request, context.settings.environment_onboarding_file, reconciling=True)
        else:
            proposal = next(
                r for r in drafts._rows(drafts._database(path)) if r["environment"] == row["environment"]
            )
            if proposal["revision"] != request["plan"]["draft_revision"]:
                raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
            from .environment_runtime import _private_json

            change, _ = _private_json(
                Path(context.settings.data_home) / "publication-bundles" / row["id"] / "source-operation.json"
            )
            if hashlib.sha256(
                Path(context.settings.environment_onboarding_file).read_bytes()
            ).hexdigest() not in {change.get("references_after"), change["references_before"]}:
                raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")
    if fresh and (row["cancel"] or request["plan"]["expires_at"] <= time.time()):
        raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    return row, request


def dispatch(context, payload):
    if set(payload) - {
        "publication",
        "id",
        "owner",
        "step",
        "result",
        "spec",
        "readiness",
        "observe_only",
        "bundle",
        "restore",
    } or payload.get("publication") not in {
        "stage",
        "intent",
        "record",
        "complete",
        "central",
        "describe",
        "connect",
        "directory",
        "capacity-reference",
        "source-configuration",
        "yield",
    }:
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    path = management._path(context)
    if payload["publication"] in {
        "describe",
        "connect",
        "directory",
        "capacity-reference",
        "source-configuration",
    }:
        with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
            row, request = current(context, conn, payload["id"])
            if (
                row["state"] != "executing"
                or row["owner"] != payload["owner"]
                or row["lease_until"] <= time.time()
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        if payload["publication"] == "describe":
            return describe(context, request, row["id"])
        if payload["publication"] in {"connect", "source-configuration", "directory"} and not (
            payload.get("restore") or request.get("restore_requested")
        ):
            with gateway_db.connection(drafts._database(path), readonly=True) as conn:
                current(context, conn, row["id"], fresh=True)
        if payload["publication"] == "connect":
            expected = request["steps"].get("central", {}).get("bundle")
            if payload["bundle"] != expected:
                raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_BUNDLE_CHANGED")
            return connect(context, payload["bundle"])
        if payload["publication"] == "source-configuration":
            return source_configuration(context, request, row["id"], restore=payload.get("restore", False))
        if payload["publication"] == "capacity-reference":
            from .environment_runtime import _private_json

            references, _ = _private_json(context.settings.environment_installation_file)
            target = next(r for r in references["targets"] if r["id"] == request["target"]["id"])
            if not target.get("capacity"):
                raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_CAPACITY_CONFIG_REQUIRED")
            return target["capacity"]
        return activate_directory(context, request, payload["result"])
    if payload["publication"] == "central":
        with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
            row, request = current(context, conn, payload["id"], fresh=True)
            if (
                row["state"] != "executing"
                or row["owner"] != payload["owner"]
                or row["lease_until"] <= time.time()
            ):
                raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        if payload["spec"] != describe(context, request, row["id"])["spec"]:
            raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_SCOPE_CHANGED")
        return central_bundle(
            context,
            request,
            payload["spec"],
            payload["readiness"],
            row["id"],
            observe_only=payload["observe_only"],
        )
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row, request = current(context, conn, payload["id"])
        if (
            row["owner"] != payload["owner"]
            or row["state"] != "executing"
            or row["lease_until"] <= time.time()
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_LEASE_LOST")
        if payload["publication"] == "stage":
            return {"request": request, "id": row["id"], "cancelled": bool(row["cancel"])}
        if payload["publication"] == "yield":
            conn.execute(
                "UPDATE environment_tasks SET state='waiting',owner='',lease_until=0,updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return tasks._public(tasks._row(conn, row["id"]))
        step = payload.get("step")
        if payload["publication"] == "intent":
            if step not in {
                "readiness",
                "capacity",
                "central",
                "target",
                "verify",
                "activate",
                "restore",
                "directory",
                "source",
            }:
                raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_STEP_INVALID")
            current(context, conn, row["id"], fresh=step != "restore")
            existing = request["steps"].get(step)
            if existing:
                return {"execute": False, "observed": existing}
            request["attempted"] = True
            request["steps"][step] = {"state": "started"}
            request["phase"] = step
            conn.execute(
                "UPDATE environment_tasks SET request=?,updated_at=? WHERE id=?",
                (json.dumps(request), time.time(), row["id"]),
            )
            return {"execute": True}
        result = payload.get("result")
        if not isinstance(result, dict) or len(json.dumps(result)) > 32768:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_RESULT_INVALID")
        if payload["publication"] == "record":
            if step not in request["steps"]:
                raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_STEP_INVALID")
            request["steps"][step] = result
            if step == "source":
                request["source_updated"] = True
            public = {
                "scope": "environment-publication",
                "expires_at": request["plan"]["expires_at"],
                "activated": result.get("activated") is True,
                "phase": result.get("phase", step),
                "business_accuracy": "unconfirmed",
                "steps": {k: public_step(v) for k, v in request["steps"].items()},
            }
            conn.execute(
                "UPDATE environment_tasks SET request=?,result=?,updated_at=? WHERE id=?",
                (json.dumps(request), json.dumps(public), time.time(), row["id"]),
            )
            return {"recorded": True}
        result = {**json.loads(row["result"]), **public_step(result)}
        state = (
            "observed"
            if result.get("activated") is True and result.get("directory") == "synchronized"
            else "cancelled"
            if result.get("phase") == "restored"
            else "checking"
            if result.get("state") == "reconcile"
            else "blocked"
        )
        conn.execute(
            "UPDATE environment_tasks SET state=?,result=?,owner='',lease_until=0,updated_at=? WHERE id=?",
            (
                state,
                json.dumps(
                    {
                        **result,
                        "scope": "environment-publication",
                        "business_accuracy": "unconfirmed",
                        "expires_at": request["plan"]["expires_at"],
                    }
                ),
                time.time(),
                row["id"],
            ),
        )
        return tasks._public(tasks._row(conn, row["id"]))


def public_step(result):
    # Public tasks never carry private paths, tokens, documents or customer question text.
    scalar = {"state", "phase", "activated", "business_accuracy", "observed_at", "directory", "error_code"}
    public = {
        key: value
        for key, value in result.items()
        if key in scalar and isinstance(value, (str, bool, int, float))
    }
    if "routes" in result:
        public["routes"] = {
            k: v for k, v in result["routes"].items() if k in {"operator", "consumer"} and type(v) is int
        }
    if "versions" in result:
        public["versions"] = {
            k: {a: b for a, b in v.items() if a in {"agent_id", "version_no", "app_id", "workflow_id"}}
            for k, v in result["versions"].items()
            if k in {"operator", "consumer"}
        }
    return public


def restore_task(context, identity, identifier):
    path = management._path(context)
    with drafts._locked(path), gateway_db.connection(drafts._database(path), write=True) as conn:
        row, request = current(context, conn, identifier)
        tasks._authorize(path, identity, row["environment"])
        if row["state"] == "executing" and row["lease_until"] > time.time():
            raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
        request["identity"] = identity
        request["restore_requested"] = True
        conn.execute(
            "UPDATE environment_tasks SET request=?,state='checking',cancel=1,owner='',lease_until=0,"
            "updated_at=? WHERE id=?",
            (json.dumps(request), time.time(), identifier),
        )
        return tasks._public(tasks._row(conn, identifier))


def central_bundle(context, request, spec, readiness, identifier, *, observe_only=False):
    """中央只冻结共享应用；私有包不出管理面，不触目标准入。"""
    import yaml

    from aiops_diagnostics.consumer.consumer_agent_flow import publication_settings
    from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
    from aiops_diagnostics.consumer.consumer_publication import flags_of, frozen_settings
    from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource, workflow_content
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.knowledge import knowledge_delegation

    from . import environment_consumer, service_keys
    from .environment_consumer_trial import candidate_workflow_sha

    settings = context.settings

    def authorize():
        with gateway_db.connection(drafts._database(management._path(context)), readonly=True) as connection:
            current(context, connection, identifier, fresh=True)

    capacity = request["steps"].get("capacity", {})
    if not (Path(settings.data_home) / "publication-bundles" / identifier / "release.json").exists() and (
        capacity.get("state") != "passed"
        or time.time() - capacity.get("observed_at", 0) > 900
        or len(capacity.get("runs", [])) != 3
    ):
        raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_CAPACITY_REQUIRED")
    directory = Path(settings.data_home) / "publication-bundles" / identifier
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    from aiops_diagnostics.common.private_files import validate_private_directory

    validate_private_directory(directory)
    intent = directory / "intent.json"
    secret = directory / "release.json"
    if secret.exists():
        from .environment_runtime import _private_json

        existing, _ = _private_json(secret)
        if any(existing[k] != spec[k] for k in ("id", "environment", "tenant_id", "case_ref", "installed")):
            raise ValueError("publication artifact scope changed")
        knowledge_delegation.active(
            settings, knowledge_delegation.parse_id(existing["knowledge"]["capabilities"][0]["knowledge_id"])
        )
        console = DifyConsoleAdmin(
            ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
        )
        app = existing["apps"]["consumer"]
        client = DifyDslClient(
            DifyDslSource(
                settings.dify_console_base_url,
                app["app_id"],
                settings.dify_console_api_key,
                existing["workspace_id"],
            )
        )
        if workflow_content(client.export_dsl(workflow_id=app["workflow_id"])) != app["workflow"] or app[
            "key"
        ] not in service_keys.list_keys(console, existing["workspace_id"], app["app_id"]):
            raise ValueError("published content or credential changed")
        return {"path": str(secret), "sha256": hashlib.sha256(secret.read_bytes()).hexdigest()}
    if observe_only and not intent.exists():
        raise ValueError("central publication outcome unknown")
    path = management._path(context)
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        consumer = json.loads(tasks._row(conn, request["children"]["consumer-prepare"])["request"])
    workspace, tenant, env = spec["consumer"]["workspace_id"], spec["tenant_id"], spec["environment"]
    library = spec["consumer"]["knowledge_base_id"]
    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )
    document = environment_consumer.document(consumer["consumer_source"], library)
    source = DifyDslSource(
        settings.dify_console_base_url, spec["consumer"]["app_id"], settings.dify_console_api_key, workspace
    )
    client = DifyDslClient(source)
    exported = client.export_dsl()
    workflow = workflow_content(exported)
    # Dify导出补回变量自身selector；比对沿用候选摘要的规范化口径，冻结体仍按导出原样保存。
    if candidate_workflow_sha(exported) != candidate_workflow_sha(yaml.safe_dump(document)):
        raise ValueError("publication draft differs from validated source")
    snapshot = {
        "environment": env,
        "tenant_id": tenant,
        "workspace_id": workspace,
        "app_id": source.app_id,
        "workflow": workflow,
    }
    if intent.exists():
        if json.loads(intent.read_text()) != snapshot:
            raise ValueError("publication intent changed")
    else:
        write_private_text(intent, json.dumps(snapshot, sort_keys=True))
    # 仅候选workspace默认凭据可切换；一个已有正式消费者占用此工作区时不能静默覆盖。
    active_apps = [
        r
        for r in SelfServiceStore(settings.database_file).apps(entry="consumer")
        if r["workspace_id"] == workspace and r["app_id"] != source.app_id
    ]
    if active_apps:
        raise ValueError("workspace has another consumer; explicit credential migration required")
    authorize()
    grants = KnowledgeGrants(settings.database_file)
    # 首次发布确认明确授予所选对客库；恢复不覆盖曾经撤销的记录。
    grants.grant(tenant, env, library, str(request["identity"][2]), backfill=True)
    if library not in grants.granted(tenant, env):
        raise tasks.TaskRefused("ENVIRONMENT_PUBLICATION_KNOWLEDGE_GRANT_REQUIRED")
    store = SelfServiceStore(settings.database_file)
    existing = store.app(source.app_id)
    if existing and (
        existing["environment"],
        existing["tenant_id"],
        existing["workspace_id"],
        existing["entry"],
    ) != (env, tenant, workspace, "consumer"):
        raise ValueError("shared application identity conflict")
    store.register(
        app_id=source.app_id,
        tenant_id=tenant,
        workspace_id=workspace,
        agent_name="consumer-" + source.app_id,
        profile_id="",
        actor=str(request["identity"][2]),
        entry="consumer",
        environment=env,
        targets={"knowledge_base_ids": [library], **flags_of(workflow)},
    )
    capability = knowledge_delegation.issue(settings, env, tenant, source.app_id, library)
    authorize()
    key = service_keys.ensure(
        console, settings.chat_dify_key_dir, workspace, source.app_id, database=settings.database_file
    )
    detail = console._call(workspace, "/console/api/apps/" + source.app_id)
    published = (detail.get("workflow") or {}).get("id")
    if published and workflow_content(client.export_dsl(workflow_id=published)) == workflow:
        workflow_id = published
    elif observe_only:
        raise ValueError("Dify publication not confirmed")
    else:
        # 原接口不对POST重试；本任务started后下轮只导出核对。
        authorize()
        if not client.publish_workflow():
            raise ValueError("Dify publication unconfirmed")
        workflow_id = client.published_workflow_id()
        if workflow_content(client.export_dsl(workflow_id=workflow_id)) != workflow:
            raise ValueError("Dify publication changed")
    detail = console._call(workspace, "/console/api/apps/" + source.app_id)
    if detail.get("enable_site") is not False:
        raise ValueError("customer site must remain disabled")
    if detail.get("enable_api") is not True:
        if observe_only:
            raise ValueError("service API activation unconfirmed")
        console._call(
            workspace,
            "/console/api/apps/" + source.app_id + "/api-enable",
            method="POST",
            body={"enable_api": True},
        )
        if console._call(workspace, "/console/api/apps/" + source.app_id).get("enable_api") is not True:
            raise ValueError("service API activation unconfirmed")
    ceiling = frozen_settings(
        {
            "environment": env,
            "tenant_id": tenant,
            "app_id": source.app_id,
            "source": "managed",
            "knowledge_base_ids": [library],
            **flags_of(workflow),
        }
    )
    content = publication_settings(
        workflow, ceiling, [{"knowledge_base_id": library, "audience": "customer"}]
    )
    release = {
        "schema": "aiops-environment-release/1",
        "id": identifier,
        "environment": env,
        "tenant_id": tenant,
        "workspace_id": workspace,
        "actor": str(request["identity"][2]),
        "installed": request["installed"],
        "validated": request["validation"],
        "capacity": capacity,
        "acceptance_ref": request["acceptance_ref"],
        "knowledge": {
            "schema": "aiops-knowledge-delegation/1",
            "base_url": settings.public_base_url.rstrip("/"),
            "capabilities": [capability],
        },
        "apps": {
            "consumer": {
                "app_id": source.app_id,
                "workflow_id": workflow_id,
                "workflow": workflow,
                "settings": content,
                "key": Path(key).read_text().strip(),
            },
            "operator": {
                "app_id": spec["operator_app"],
                "manifest": readiness["operator_manifest"],
                "prompt": readiness["operator_prompt"],
            },
        },
        "expires_at": request["plan"]["expires_at"],
        "gateway_url": readiness["gateway_url"],
        "catalogue_token": readiness["catalogue_token"],
        "case_ref": request["case_ref"],
        "dify_chat_base_url": readiness["dify_chat_base_url"],
    }
    write_private_text(secret, json.dumps(release, sort_keys=True) + "\n")
    return {"path": str(secret), "sha256": hashlib.sha256(secret.read_bytes()).hexdigest()}


def connect(context, bundle):
    """目标受限知识已安装后再切候选插件回调，不能先验证尚未就绪的目标。"""
    from types import SimpleNamespace

    from .dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from .environment_runtime import _private_json
    from .self_service_prepare import consumer_plugin

    doc, digest = _private_json(bundle["path"])
    if digest != bundle["sha256"]:
        raise ValueError("release bundle changed")
    console = DifyConsoleAdmin(
        ConsoleApiSettings(context.settings.dify_console_base_url, context.settings.dify_console_api_key)
    )
    consumer_plugin(
        console,
        doc["workspace_id"],
        SimpleNamespace(public_base_url=doc["gateway_url"]),
        doc["catalogue_token"],
        {"workflow": doc["apps"]["consumer"]["workflow"]},
    )
    return {"connected": True}


def describe(context, request, identifier):
    import yaml

    from .environment_consumer import document
    from .environment_consumer_trial import candidate_workflow_sha

    path = management._path(context)
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        operator = tasks._row(conn, request["children"]["operator-prepare"])
        consumer = tasks._row(conn, request["children"]["consumer-prepare"])
    cr, co = json.loads(consumer["request"]), json.loads(consumer["result"])
    scope = {k: cr[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id")}
    scope.update(
        environment=request["plan"]["environment"],
        app_id=co["app_id"],
        task_id=consumer["id"],
        plan_id=request["plan"]["id"],
        order_no="",
        workflow_sha256=candidate_workflow_sha(
            yaml.safe_dump(document(cr["consumer_source"], scope["knowledge_base_id"]))
        ),
    )
    directory = Path(context.settings.data_home) / "publication-bundles" / identifier
    return {
        # 中央步工件标记：intent.json 是首个落盘点，两者皆无证明未越过比对、无共享副作用。
        "markers": {
            "intent": (directory / "intent.json").exists(),
            "release": (directory / "release.json").exists(),
        },
        "spec": {
            "id": identifier,
            "environment": request["plan"]["environment"],
            "tenant_id": request["tenant_id"],
            "case_ref": request["case_ref"],
            "capabilities": request["capabilities"],
            "installed": request["installed"],
            "operator_app": json.loads(operator["result"])["app_id"],
            "consumer": scope,
        },
    }


def activate_directory(context, request, result):
    from aiops_diagnostics.gateway import environment_registry as registry

    restore = result.get("phase") == "restored"
    if not restore and (result.get("activated") is not True or result.get("phase") != "activated"):
        raise ValueError("target admission not confirmed")
    path = management._path(context)
    with drafts._locked(path):
        current = registry.load(path)
        tasks._authorize(path, tuple(request["identity"]), request["plan"]["environment"])
        source = next(
            r
            for r in drafts._rows(drafts._database(path))
            if r["environment"] == request["plan"]["environment"]
        )
        draft = json.loads(source["body"])
        incoming = {
            k: v for k, v in draft.items() if k not in {"timezone", "deployment_target", "credential_ref"}
        }
        incoming["config_ref"] = f"/etc/aiops-access-{incoming['id']}/production.env"
        row = next((r for r in current["environments"] if r["id"] == incoming["id"]), None)
        if row is None and restore:
            return {"revision": current["revision"]}
        if row is None:
            row = incoming
            row["tenants"] = [{**t, "enabled": False} for t in row["tenants"]]
            current["environments"].append(row)
        if any(row[k] != incoming[k] for k in ("id", "gateway_url", "company_gateway_url", "cpms_base_url")):
            raise ValueError("central directory connection changed")
        selected = next((t for t in row["tenants"] if t["id"] == request["tenant_id"]), None)
        if selected is None:
            if restore:
                return {"revision": current["revision"]}
            approved = next((t for t in incoming["tenants"] if t["id"] == request["tenant_id"]), None)
            if approved is None:
                raise ValueError("approved tenant is absent")
            selected = {**approved, "enabled": False}
            row["tenants"].append(selected)
        if selected["enabled"] is not restore:
            return {"revision": current["revision"]}
        if not restore:
            row["enabled"] = True
        selected["enabled"] = not restore
    revision = registry.apply(
        Path(path),
        current["environments"],
        expected_revision=current["revision"],
        actor="environment-publication",
        managers=current["managers"],
    )
    return {"revision": revision}


def recover_source_reference(context, request, state):
    import copy

    refs = Path(context.settings.environment_onboarding_file)
    actual = json.loads(refs.read_text())
    previous = copy.deepcopy(state["reference_document"])
    selected = next(r for r in actual["targets"] if r["id"] == request["target"]["id"])
    config = next(c for c in selected["configurations"] if c["id"] == state["after"]["id"])
    if (
        config["sha256"] != state["after"]["sha256"]
        or hashlib.sha256(plans._private(config["path"]).read_bytes()).hexdigest() != config["sha256"]
    ):
        raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")
    original = next(r for r in previous["targets"] if r["id"] == request["target"]["id"])
    original_config = next(c for c in original["configurations"] if c["id"] == config["id"])
    original_config.update(config)
    if actual != previous:
        raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")


def source_configuration(context, request, identifier, *, restore=False):
    """源引用和目标事务分别CAS；原不可变模板留存，失败只核对本次转换。"""
    from dotenv import dotenv_values

    from .environment_runtime import _private_json

    path = management._path(context)
    refs = Path(context.settings.environment_onboarding_file)
    directory = Path(context.settings.data_home) / "publication-bundles" / identifier
    transaction = directory / "source-operation.json"
    draft = json.loads(
        next(
            r
            for r in drafts._rows(drafts._database(path))
            if r["environment"] == request["plan"]["environment"]
        )["body"]
    )
    entries = plans._references(refs)
    target = next(t for t in entries if t["id"] == draft["deployment_target"])
    source = next(c for c in target["configurations"] if c["id"] == draft["credential_ref"])
    if transaction.exists():
        state, _ = _private_json(transaction)
    elif restore:
        return {"state": "unchanged"}
    else:
        if hashlib.sha256(refs.read_bytes()).hexdigest() != request["references_sha256"]:
            raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")
        current = plans._private(source["path"])
        values = dict(dotenv_values(current, interpolate=False))
        values.update(
            {
                "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": "",
                "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE": (
                    f"/etc/aiops-access-{draft['id']}/knowledge/knowledge-delegation.json"
                ),
            }
        )
        new = directory / "source.env"
        write_private_text(
            new, "".join(k + "=" + json.dumps(v) + "\n" for k, v in sorted(values.items()) if v is not None)
        )
        state = {
            "before": dict(source),
            "after": {
                "id": source["id"],
                "path": str(new),
                "sha256": hashlib.sha256(new.read_bytes()).hexdigest(),
            },
            "references_before": request["references_sha256"],
            "reference_document": json.loads(refs.read_text()),
        }
        write_private_text(transaction, json.dumps(state))
    desired, previous = (state["before"], state["after"]) if restore else (state["after"], state["before"])
    if source["sha256"] == desired["sha256"]:
        if not restore and not state.get("references_after"):
            state["references_after"] = hashlib.sha256(refs.read_bytes()).hexdigest()
            write_private_text(transaction, json.dumps(state))
        return {"state": "unchanged"}
    if source["sha256"] != previous["sha256"]:
        raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")
    expected = state.get("references_after") if restore else state["references_before"]
    if expected is None:
        raise tasks.TaskRefused("ENVIRONMENT_REFERENCES_CHANGED")
    updated = plans.update_artifact_references(
        refs,
        registry_path=path,
        identity=tuple(request["identity"]),
        environment=draft["id"],
        expected_sha256=expected,
        configuration={**desired, "expected_sha256": previous["sha256"]},
    )
    state["references_restored" if restore else "references_after"] = updated["references_sha256"]
    write_private_text(transaction, json.dumps(state))
    return {"state": "updated", "references_sha256": updated["references_sha256"]}
