"""草稿与固定模板生成停用候选；文件投影不安装、不授予业务准入。"""

import hashlib
import io
import json
import re
from pathlib import Path

from dotenv.parser import parse_stream

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.onboarding import drafts, registry


def render(draft, template, expected_sha256, at):
    drafts.validate(draft)
    template = Path(template)
    if not template.is_absolute() or any(p.is_symlink() for p in template.parents):
        raise ValueError("invalid template path")
    validate_private_file(template)
    with template.open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("template integrity not confirmed")
    values = {}
    for binding in parse_stream(io.StringIO(raw.decode())):
        if binding.error:
            raise ValueError("invalid template syntax")
        if binding.key is None:
            continue
        if (
            not re.fullmatch(r"AIOPS_[A-Z0-9_]+", binding.key)
            or binding.key in values
            or binding.value is None
            or "${" in binding.value
            or "\0" in binding.value
        ):
            raise ValueError("invalid template assignment")
        values[binding.key] = binding.value
    generated = {
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": draft["id"],
        "AIOPS_GATEWAY_PUBLIC_BASE_URL": draft["gateway_url"],
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": draft["cpms_base_url"],
        "AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE": (
            f"/etc/aiops-access-{draft['id']}/registry/environment.json"
        ),
        "AIOPS_UPMS_BASE_URL": draft["company_gateway_url"] + "/upms",
    }
    reserved = {
        "AIOPS_HOME",
        "AIOPS_DATA_HOME",
        "AIOPS_CONFIG_HOME",
        "AIOPS_CONFIG_FILE",
        "AIOPS_GATEWAY_DATA_HOME",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_AGENT_RUN_ROOT",
        "AIOPS_CODEX_KEY_DIR",
        "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE",
        "AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR",
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
        "AIOPS_GATEWAY_ENVIRONMENT",
        "AIOPS_GATEWAY_ENVIRONMENT_MANAGEMENT_REGISTRY_FILE",
        "AIOPS_GATEWAY_ENVIRONMENT_ONBOARDING_FILE",
        # 租户模板不能重定向控制面的节点安装引用。
        "AIOPS_GATEWAY_ENVIRONMENT_INSTALLATION_FILE",
        "AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE",
        "AIOPS_GATEWAY_COMPANY_GATEWAY_TRUST_FILE",
        "AIOPS_TDENGINE_URL",
        "AIOPS_TDENGINE_USER",
        "AIOPS_TDENGINE_PASSWORD",
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE",
        "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE",
        "AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE",
        "AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE",
        "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE",
        "AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE",
        *generated,
    }
    if (
        not draft["company_gateway_url"]
        or any(values.get(key) for key in reserved)
        or values.get("AIOPS_SSH_ENABLED", "false").strip().lower() not in {"", "false", "0", "no", "off"}
    ):
        raise ValueError("template cannot override environment-owned configuration")
    values = {key: value for key, value in values.items() if key not in reserved}
    values.update(generated)
    values["AIOPS_SSH_ENABLED"] = "false"
    values["AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED"] = "false"
    values["AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED"] = "false"
    from aiops_diagnostics.gateway.gateway_config import SETTING_SOURCES, _parse_source_value

    for source in SETTING_SOURCES.values():
        if source.env in values:
            if not source.file:
                raise ValueError("process-only configuration belongs to installer")
            _parse_source_value(source, values[source.env])
    config = "".join(
        key
        + '="'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")
        + '"\n'
        for key, value in sorted(values.items())
    ).encode()
    row = {
        key: value
        for key, value in draft.items()
        if key not in {"deployment_target", "credential_ref", "timezone"}
    }
    row.update(enabled=False, config_ref=f"/etc/aiops-access-{draft['id']}/production.env")
    row["tenants"] = [{**tenant, "enabled": False} for tenant in row["tenants"]]
    registry.validate([row], managed=True)
    body = {
        "schema": registry.MANAGED_SCHEMA,
        "revision": 1,
        "audit": [{"revision": 1, "actor": "candidate-projection", "at": at}],
        "managers": [],
        "environments": [row],
    }
    files = {"production.env": config, "environment.json": json.dumps(body, sort_keys=True).encode() + b"\n"}
    if len(config) > 1048576 or len(files["environment.json"]) > registry.MAX_BYTES:
        raise ValueError("projection too large")
    return files


def summary(files):
    checksums = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    return {
        "projection_sha256": hashlib.sha256(json.dumps(checksums, sort_keys=True).encode()).hexdigest(),
        "projected_config_sha256": checksums["production.env"],
        "projected_registry_sha256": checksums["environment.json"],
    }


def validate_candidate(
    config, directory, environment, *, runtime=None, candidate_tenant=None, previous_directory=None
):
    """候选venv按实际配置/目录核对固定路径及停用范围，不调用外部服务。"""
    from dotenv import dotenv_values

    values = dotenv_values(config, interpolate=False)
    keys = set()
    for binding in parse_stream(io.StringIO(Path(config).read_text())):
        if binding.error or (binding.key is not None and (binding.value is None or binding.key in keys)):
            raise ValueError("invalid candidate config syntax")
        if binding.key is not None:
            keys.add(binding.key)
    text = Path(config).read_text()
    if "${" in text:
        raise ValueError("candidate config interpolation refused")
    for key in (
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE",
        "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE",
        "AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE",
        "AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE",
        "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE",
        "AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE",
        "AIOPS_TDENGINE_URL",
        "AIOPS_TDENGINE_USER",
        "AIOPS_TDENGINE_PASSWORD",
    ):
        if values.get(key) and not (
            runtime is not None
            and key
            in {
                "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE",
                "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE",
            }
        ):
            raise ValueError("candidate execution bindings must remain disabled")
    body = registry.load(directory)
    if body["schema"] != registry.MANAGED_SCHEMA or body["managers"] or len(body["environments"]) != 1:
        raise ValueError("candidate directory is not an isolated environment")
    row = body["environments"][0]
    preserving = previous_directory is not None
    if preserving:
        previous = registry.load(previous_directory)
        old = previous["environments"][0]
        if len(previous["environments"]) != 1 or any(
            row[k] != old[k]
            for k in ("id", "enabled", "gateway_url", "cpms_base_url", "company_gateway_url", "config_ref")
        ):
            raise ValueError("existing environment identity or admission changed")
        tenants = {t["id"]: t for t in row["tenants"]}
        if any(tenants.get(t["id"]) != t for t in old["tenants"]) or any(
            t["enabled"] for t in row["tenants"] if t["id"] not in {v["id"] for v in old["tenants"]}
        ):
            raise ValueError("existing tenant admission changed")
    if candidate_tenant is not None and not any(
        t["id"] == candidate_tenant and not t["enabled"] for t in row["tenants"]
    ):
        raise ValueError("candidate tenant must remain disabled")
    expected = {
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": environment,
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": row["cpms_base_url"],
        "AIOPS_GATEWAY_PUBLIC_BASE_URL": row["gateway_url"],
        "AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE": (
            f"/etc/aiops-access-{environment}/registry/environment.json"
        ),
        "AIOPS_UPMS_BASE_URL": row["company_gateway_url"] + "/upms",
        "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "false",
        "AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED": "false",
        "AIOPS_SSH_ENABLED": "false",
    }
    if runtime is not None:
        root = f"/etc/aiops-access-{environment}/runtime"
        expected.update(
            {
                "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE": root + "/operator-profiles.json",
                "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE": root + "/consumer-targets.json",
            }
        )
        validate_runtime(runtime, row)
        if preserving:
            candidate_id = json.loads((Path(runtime) / "manifest.json").read_text())["tenant_id"]
            if any(t["id"] == candidate_id and t["enabled"] for t in row["tenants"]):
                original = Path(previous_directory).parent.parent / "runtime/runtime.tar"
                if (
                    not original.is_file()
                    or original.read_bytes() != (Path(runtime) / "runtime.tar").read_bytes()
                ):
                    raise ValueError("candidate installation cannot replace an active tenant")
    if preserving or candidate_tenant is not None:
        for key in ("AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED", "AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED"):
            if values.get(key) not in {"false", "true"}:
                raise ValueError("invalid retained consumer setting")
            expected[key] = values[key]
    if (
        row["id"] != environment
        or not (preserving or candidate_tenant is not None)
        and (row["enabled"] or any(tenant["enabled"] for tenant in row["tenants"]))
        or row["config_ref"] != f"/etc/aiops-access-{environment}/production.env"
        or any(values.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("candidate configuration and directory differ or grant admission")


def validate_runtime(path, row):
    """核暂存字节与固定逻辑路径；完整profile加载由隔离候选启动核对。"""
    from types import SimpleNamespace

    from aiops_diagnostics.consumer.consumer_publication import declared_rows
    from aiops_diagnostics.execution.agent_runs import AgentRunConfig
    from aiops_diagnostics.execution.execution_manifest import from_text
    from aiops_diagnostics.onboarding.artifacts import runtime_files

    path = Path(path)
    validate_private_file(path / "runtime.tar")
    sha = hashlib.sha256((path / "runtime.tar").read_bytes()).hexdigest()
    files, body = runtime_files(path / "runtime.tar", sha, row["id"])
    for name, raw in files.items():
        validate_private_file(path / name)
        if (path / name).read_bytes() != raw:
            raise ValueError("runtime snapshot differs from archive")
    if {p.name for p in path.iterdir()} != set(files) | {"runtime.tar"}:
        raise ValueError("runtime snapshot contains unknown files")
    registration = json.loads(files["registrations.json"])
    if (
        registration.get("environment") != row["id"]
        or registration.get("enabled") is not False
        or registration.get("gateway_url") != row["gateway_url"]
        or registration.get("tenant_id") != body["tenant_id"]
        or body["tenant_id"] not in {t["id"] for t in row["tenants"]}
        or not all(
            isinstance(registration.get(k), str) and registration[k]
            for k in (
                "workspace_id",
                "operator_app_id",
                "consumer_app_id",
            )
        )
        or registration["operator_app_id"] == registration["consumer_app_id"]
    ):
        raise ValueError("runtime registration scope mismatch")
    config = AgentRunConfig.load(path / "agent-runs.json")
    if config.environment != row["id"] or any(
        node.environment != row["id"] for node in config.node_token_hashes.values()
    ):
        raise ValueError("runtime node environment mismatch")
    manifest = from_text(files["operator-manifest.json"].decode())
    from aiops_diagnostics.operator import operator_agent, operator_tools

    expected_manifest = json.loads(files["operator-manifest.json"])
    if (
        expected_manifest["schema"] != operator_agent.SCHEMA
        or expected_manifest["system_messages"] != [operator_agent.sop() + "\n" + operator_agent.INSTRUCTIONS]
        or expected_manifest["tools"]
        != [
            dict(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in operator_tools.tools()
        ]
    ):
        raise ValueError("runtime operator protocol mismatch")
    profile = json.loads(files["operator-profiles.json"])
    root = f"/etc/aiops-access-{row['id']}/runtime"
    if profile.get("schema") != "operator-profiles/1" or len(profile.get("profiles", [])) != 1:
        raise ValueError("runtime profile shape mismatch")
    profile = profile["profiles"][0]
    target = profile["target"]
    if (
        profile["id"] != "operator-" + row["id"]
        or target.get("environment") != row["id"]
        or target.get("entry") != "operator"
        or target.get("cpms_base_url") != row["cpms_base_url"]
        or target.get("config_file") != root + "/agent-runs.json"
        or target.get("manifest_file") != root + "/operator-manifest.json"
        or profile["model"].get("credential_file") != root + "/operator-model.json"
        or manifest.model_key != profile["model"]["name"]
        or any(
            m.get("credential_file") != root + "/consumer-model.json"
            for m in profile.get("consumer_models", [])
        )
    ):
        raise ValueError("runtime profile scope mismatch")
    from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url

    canonical_gateway_url(target["proxy_base_url"])
    consumers = declared_rows(
        SimpleNamespace(consumer_publication_targets_file=str(path / "consumer-targets.json"))
    )
    if not consumers or body["schema"] == "aiops-environment-runtime/1" and len(consumers) != 1:
        raise ValueError("runtime consumer shape mismatch")
    if any(
        c["environment"] != row["id"] or c["tenant_id"] not in {t["id"] for t in row["tenants"]}
        for c in consumers
    ):
        raise ValueError("runtime retained target scope mismatch")
    matches = [c for c in consumers if c["tenant_id"] == body["tenant_id"]]
    if len(matches) != 1:
        raise ValueError("runtime candidate target missing")
    consumer = matches[0]
    if (
        consumer.get("environment") != row["id"]
        or consumer.get("tenant_id") != body["tenant_id"]
        or consumer.get("app_id") != registration["consumer_app_id"]
        or not isinstance(consumer.get("knowledge_base_ids"), list)
        or len(consumer["knowledge_base_ids"]) != 1
        or any(type(consumer.get(k)) is not bool for k in ("live_order_status", "live_billing_record"))
    ):
        raise ValueError("runtime consumer scope mismatch")
