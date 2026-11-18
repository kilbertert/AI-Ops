"""草稿与固定模板生成停用候选；文件投影不安装、不授予业务准入。"""

import hashlib
import io
import json
import re
from pathlib import Path

from dotenv.parser import parse_stream

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_registry as registry


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


def validate_candidate(config, directory, environment):
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
        if values.get(key):
            raise ValueError("candidate execution bindings must remain disabled")
    body = registry.load(directory)
    if body["schema"] != registry.MANAGED_SCHEMA or body["managers"] or len(body["environments"]) != 1:
        raise ValueError("candidate directory is not an isolated environment")
    row = body["environments"][0]
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
    if (
        row["id"] != environment
        or row["enabled"]
        or any(tenant["enabled"] for tenant in row["tenants"])
        or row["config_ref"] != f"/etc/aiops-access-{environment}/production.env"
        or any(values.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("candidate configuration and directory differ or grant admission")
