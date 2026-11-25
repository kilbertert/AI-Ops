"""已准备双端资源的私有运行工件；导出不安装、不授予业务准入。"""

import hashlib
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url
from aiops_diagnostics.common.private_files import validate_private_directory, validate_private_file
from aiops_diagnostics.execution import execution_manifest
from aiops_diagnostics.execution.agent_runs import AgentRunConfig
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.operator import operator_agent, operator_tools
from aiops_diagnostics.operator.ops_provider import ensure_node_provider

from . import candidate_knowledge, environment_consumer, environment_operator
from .dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin


def _private_json(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in path.parents):
        raise ValueError("runtime reference must be an absolute private path")
    validate_private_file(path)
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError("runtime reference too large")
    return json.loads(raw, object_pairs_hook=registry._unique_object), hashlib.sha256(raw).hexdigest()


def _applications(settings, payload, *, allow_renewal=False):
    if set(payload) != {"identity", "plan_id", "tenant_id", "operator_task", "consumer_task"}:
        raise ValueError("runtime request invalid")
    identity = payload["identity"]
    if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(v, str) for v in identity):
        raise ValueError("runtime identity invalid")
    path = settings.environment_management_registry_file or settings.environment_registry_file
    plan = plans.read(path, tuple(identity), payload["plan_id"], settings.environment_onboarding_file)
    if plan["state"] == "expired" or any(
        c["state"] != "passed"
        for c in plan["checks"]
        if c["id"] not in {"host-identity", "capacity", "network", "business"}
    ):
        raise ValueError("runtime plan expired")
    resources = {}
    source_plan_ids: set[str] = set()
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        for entry in ("operator", "consumer"):
            row = tasks._row(conn, payload[entry + "_task"])
            request, result = json.loads(row["request"]), json.loads(row["result"])
            tasks._authorize(path, tuple(identity), row["environment"])
            tasks._authorize(path, tuple(request["identity"]), row["environment"])
            tasks._current(path, request, settings.environment_onboarding_file, reconciling=True)
            renewal = allow_renewal and plan["id"] != request["plan"]["id"]
            if renewal:
                source_plan = conn.execute(
                    "SELECT environment,digest FROM environment_plans WHERE id=?",
                    (request["plan"]["id"],),
                ).fetchone()
                if (
                    source_plan is None
                    or source_plan["environment"] != plan["environment"]
                    or source_plan["digest"] != plan["digest"]
                ):
                    raise ValueError("runtime application observation required")
            observed_state = row["state"] if renewal else tasks._public(row)["state"]
            if (
                observed_state != "observed"
                or row["cancel"]
                or request.get("action") != entry + "-prepare"
                or request.get("tenant_id") != payload["tenant_id"]
                or request["plan"]["environment"] != plan["environment"]
                or request["plan"]["digest"] != plan["digest"]
                or (not renewal and request["plan"]["id"] != plan["id"])
                or result.get("environment") != plan["environment"]
                or result.get("tenant_id") != payload["tenant_id"]
                or not result.get("app_id")
                or result["app_id"] != request.get("resources", {}).get("expected_app_id")
                or not request.get("resources", {}).get("attempted")
            ):
                raise ValueError("runtime application observation required")
            source_plan_ids.add(request["plan"]["id"])
            resources[entry] = {"request": request, "result": result}
        if len(source_plan_ids) != 1:
            raise ValueError("runtime application observation required")
        draft = json.loads(
            next(r for r in drafts._rows(drafts._database(path)) if r["environment"] == plan["environment"])[
                "body"
            ]
        )
    scopes = {(r["request"]["workspace_id"], r["request"]["knowledge_base_id"]) for r in resources.values()}
    if len(scopes) != 1:
        raise ValueError("runtime resource scopes differ")
    workspace, library = next(iter(scopes))
    candidate_knowledge.active(settings, (plan["environment"], payload["tenant_id"], workspace, library))
    return plan, draft, resources


def render(settings, payload, node_file, *, allow_renewal=False):
    plan, draft, apps = _applications(settings, payload, allow_renewal=allow_renewal)
    node, node_digest = _private_json(node_file)
    if (
        set(node) != {"environment", "config_file", "manifest_file", "proxy_base_url"}
        or node["environment"] != draft["id"]
    ):
        raise ValueError("runtime node environment invalid")
    config, config_digest = _private_json(node["config_file"])
    template, template_digest = _private_json(node["manifest_file"])
    parsed = AgentRunConfig.load(Path(node["config_file"]))
    execution_manifest.load(node["manifest_file"])
    if parsed.environment != draft["id"] or not any(
        n.active and n.environment == parsed.environment and n.node_id == parsed.expected_node_id
        for n in parsed.node_token_hashes.values()
    ):
        raise ValueError("runtime node identity invalid")
    if canonical_gateway_url(node["proxy_base_url"]) != "https://gateway-egress:9443":
        raise ValueError("runtime callback must use the dedicated environment egress")
    from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint

    require_secure_endpoint(template["model"]["endpoint"], what="节点模型出口")
    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )
    model = environment_operator.model(settings)
    for entry, resource in apps.items():
        request, result = resource["request"], resource["result"]
        scope = (draft["id"], payload["tenant_id"], request["workspace_id"], request["knowledge_base_id"])
        expected_binding = (
            environment_operator.binding(settings)
            if entry == "operator"
            else environment_consumer.binding(settings, request["consumer_source"])
        )
        if request["binding"] != expected_binding:
            raise ValueError("runtime application template changed")
        app = console._call(scope[2], "/console/api/apps/" + result["app_id"])
        if (
            app.get("enable_api") is not False
            or app.get("enable_site") is not False
            or app.get("id") != result["app_id"]
            or app.get("name") != request["resources"]["name"]
            or app.get("mode") != ("agent-chat" if entry == "operator" else "advanced-chat")
            or app.get("workflow")
            or console._call(scope[2], "/console/api/apps/" + result["app_id"] + "/api-keys").get("data")
            != []
        ):
            raise ValueError("runtime candidate has active entries")
        if entry == "consumer":
            environment_consumer.verify(console, settings, request, scope, result["app_id"])
        else:
            mc = app.get("model_config") or {}
            from .dify_dsl_pull import _declared_dataset_ids

            tools = mc.get("agent_mode", {}).get("tools", [])
            if (
                app.get("mode") != "agent-chat"
                or mc.get("pre_prompt") != operator_agent.sop()
                or mc.get("model", {}).get("name") != model["name"]
                or mc.get("model", {}).get("provider") != model["provider"]
                or mc.get("agent_mode", {}).get("enabled") is not True
                or mc.get("agent_mode", {}).get("strategy") != "function_call"
                or mc.get("agent_mode", {}).get("max_iteration") != 10
                or mc.get("model", {}).get("completion_params") != model["completion_params"]
                or len(tools) != len(operator_tools.tools())
                or {t.get("tool_name") for t in tools} != set(operator_tools.names())
                or _declared_dataset_ids(mc)
                or any(
                    t.get("provider_id") != request["resources"]["provider_id"]
                    or t.get("provider_type") != "api"
                    or t.get("enabled") is not True
                    for t in tools
                )
            ):
                raise ValueError("runtime operator application changed")
            ensure_node_provider(console, scope[2], draft["gateway_url"], verify_only=True)
    options = dict(model["completion_params"])
    options["stop_sequences"] = options.pop("stop", [])
    manifest = {
        "schema": operator_agent.SCHEMA,
        "name": "operator-" + draft["id"],
        "history_policy": operator_agent.HISTORY_POLICY,
        "model": {key: template["model"][key] for key in ("provider_key", "adapter_key", "endpoint")}
        | {"model_key": model["name"]},
        "model_options": options,
        "system_messages": [operator_agent.sop() + "\n" + operator_agent.INSTRUCTIONS],
        "tools": [
            dict(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in operator_tools.tools()
        ],
    }
    execution_manifest.from_text(json.dumps(manifest))
    root = "/etc/aiops-access-" + draft["id"] + "/runtime"
    target = {
        "environment": draft["id"],
        "entry": "operator",
        "config_file": root + "/agent-runs.json",
        "manifest_file": root + "/operator-manifest.json",
        "cpms_base_url": draft["cpms_base_url"],
        "proxy_base_url": node["proxy_base_url"],
    }
    consumer = apps["consumer"]["request"]
    consumer_model = environment_consumer.profile(settings, consumer["consumer_source"])["model"]
    consumer_target = {
        "environment": draft["id"],
        "tenant_id": payload["tenant_id"],
        "app_id": apps["consumer"]["result"]["app_id"],
        "knowledge_base_ids": [consumer["knowledge_base_id"]],
    }
    from aiops_diagnostics.consumer.consumer_publication import flags_of

    consumer_target.update(flags_of(consumer["consumer_source"]["workflow"]))
    bodies = {
        "agent-runs.json": config,
        "operator-manifest.json": manifest,
        "operator-profiles.json": {
            "schema": "operator-profiles/1",
            "profiles": [
                {
                    "id": "operator-" + draft["id"],
                    "model": {**model, "credential_file": root + "/operator-model.json"},
                    "consumer_models": [{**consumer_model, "credential_file": root + "/consumer-model.json"}]
                    if consumer_model != model
                    else [],
                    "target": target,
                }
            ],
        },
        "consumer-targets.json": {"schema": "aiops-consumer-targets/1", "targets": [consumer_target]},
        "registrations.json": {
            "environment": draft["id"],
            "gateway_url": draft["gateway_url"],
            "tenant_id": payload["tenant_id"],
            "workspace_id": consumer["workspace_id"],
            "operator_app_id": apps["operator"]["result"]["app_id"],
            "consumer_app_id": consumer_target["app_id"],
            "enabled": False,
        },
        "operator-model.json": _private_json(model["credential_file"])[0],
        "consumer-model.json": _private_json(consumer_model["credential_file"])[0],
    }
    result = {
        "schema": "aiops-environment-runtime/1",
        "scope": "runtime-wiring-only",
        "environment": draft["id"],
        "tenant_id": payload["tenant_id"],
        "plan_id": plan["id"],
        "plan_digest": plan["digest"],
        "node_reference_sha256": node_digest,
        "node_config_sha256": config_digest,
        "node_manifest_sha256": template_digest,
        "executable": False,
        "installed": False,
        "activated": False,
        "checks": [
            *[
                {"id": k, "state": "passed"}
                for k in ("resource-scope", "node-binding", "application-content")
            ],
            *[{"id": k, "state": "pending"} for k in ("consumer-callback", "knowledge-content")],
            *[
                {"id": k, "state": "pending"}
                for k in ("installation", "candidate-runtime", "business", "activation")
            ],
        ],
    }
    files = {name: (json.dumps(body, sort_keys=True).encode() + b"\n") for name, body in bodies.items()}
    result["files"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    files["manifest.json"] = json.dumps(result, sort_keys=True).encode() + b"\n"
    return files, result


def export(settings, payload, node_file, output, *, allow_renewal=False):
    output = Path(output)
    if (
        not output.is_absolute()
        or any(p.is_symlink() for p in output.parents)
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError("runtime output must be new and private")
    validate_private_directory(output.parent)
    files, result = render(settings, payload, node_file, allow_renewal=allow_renewal)
    descriptor, filename = tempfile.mkstemp(dir=output.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w") as bundle:
                for name, raw in sorted(files.items()):
                    member = tarfile.TarInfo(name)
                    member.mode, member.size = 0o600, len(raw)
                    bundle.addfile(member, io.BytesIO(raw))
            stream.flush()
            os.fsync(stream.fileno())
        latest, _ = render(settings, payload, node_file, allow_renewal=allow_renewal)
        if latest != files:
            raise ValueError("runtime inputs changed during export")
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return {k: v for k, v in result.items() if k != "files"} | {
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest()
    }


def register_installed(settings, expected_sha256, receipt_fd, *, observe_only=False):
    """受管安装器以目标服务身份调用；只消费现役私有工件，不接共享管理凭据。"""
    import fcntl
    import stat

    from aiops_diagnostics.gateway import gateway_schema
    from aiops_diagnostics.gateway.environment_artifacts import runtime_files
    from aiops_diagnostics.gateway.environment_projection import validate_candidate

    from .self_service_config import profiles
    from .self_service_store import SelfServiceStore

    root = Path(f"/etc/aiops-access-{settings.environment}")
    app = Path(f"/opt/aiops-access-{settings.environment}")
    receipt_path = app / "installed.json"
    observed = os.fstat(receipt_fd)
    if (
        observed.st_uid != 0
        or observed.st_mode & 0o077
        or observed.st_nlink != 1
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_size > 65536
        or fcntl.fcntl(receipt_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        or receipt_path.is_symlink()
        or (observed.st_dev, observed.st_ino) != (receipt_path.stat().st_dev, receipt_path.stat().st_ino)
    ):
        raise ValueError("root-owned installation receipt descriptor required")
    with os.fdopen(os.dup(receipt_fd), "rb") as stream:
        stream.seek(0)
        installed = json.load(stream)
    runtime = root / "runtime"
    if (
        settings.execution_environment != settings.environment
        or not settings.server_config_file
        or Path(settings.server_config_file) != root / "production.env"
        or settings.environment_registry_file != str(root / "registry/environment.json")
        or settings.operator_profiles_file != str(runtime / "operator-profiles.json")
        or settings.consumer_publication_targets_file != str(runtime / "consumer-targets.json")
        or str(settings.database_file) != f"/var/lib/aiops-access-{settings.environment}/gateway/gateway.db"
    ):
        raise ValueError("runtime registration must use installed environment paths")
    validate_private_file(Path(settings.database_file))
    report = gateway_schema.check(settings.database_file)
    if report["version"] != report["current_version"]:
        raise ValueError("runtime registration requires current schema")

    def current():
        latest = receipt_path.stat()
        if receipt_path.is_symlink() or (latest.st_dev, latest.st_ino) != (observed.st_dev, observed.st_ino):
            raise ValueError("installation receipt replaced during registration")
        if (app / "pending.json").exists():
            raise ValueError("unfinished installation prevents registration")
        if (
            installed["environment"] != settings.environment
            or installed["runtime_sha256"] != expected_sha256
            or (app / "current").resolve() != app / "releases" / installed["release"]
            or runtime.resolve() != app / "releases" / installed["runtime_release"] / "runtime"
            or json.loads((app / "current/manifest.json").read_text())["revision"] != installed["revision"]
            or json.loads((app / "current/manifest.json").read_text())["version"] != installed["version"]
            or hashlib.sha256((root / "production.env").read_bytes()).hexdigest()
            != installed["config_sha256"]
            or hashlib.sha256((root / "registry/environment.json").read_bytes()).hexdigest()
            != installed["registry_sha256"]
        ):
            raise ValueError("installed runtime receipt mismatch")
        files, manifest = runtime_files(runtime / "runtime.tar", expected_sha256, settings.environment)
        validate_candidate(
            root / "production.env",
            root / "registry/environment.json",
            settings.environment,
            runtime=runtime,
            candidate_tenant=manifest["tenant_id"],
        )
        if set(profiles(settings)) != {"operator-" + settings.environment}:
            raise ValueError("runtime profile mismatch")
        before = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("production.env", "registry/environment.json")
        }
        return files, manifest, before, str(runtime.resolve())

    files, manifest, config_hashes, pointer = current()
    registration = json.loads(files["registrations.json"])
    consumer = next(
        c
        for c in json.loads(files["consumer-targets.json"])["targets"]
        if c["tenant_id"] == manifest["tenant_id"]
    )

    def unchanged():
        if current() != (files, manifest, config_hashes, pointer):
            raise ValueError("runtime changed during registration")

    store = SelfServiceStore(settings.database_file, readonly=observe_only)
    changed = store.register_runtime(registration, consumer, verify=unchanged, observe_only=observe_only)
    return {
        "environment": settings.environment,
        "tenant_id": manifest["tenant_id"],
        "runtime_sha256": expected_sha256,
        "result": "absent" if changed is None else "registered" if changed else "unchanged",
        "scope": "inactive-runtime-registration-only",
        "activated": False,
        "business_acceptance": False,
    }
