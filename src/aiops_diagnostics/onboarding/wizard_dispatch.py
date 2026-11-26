"""V2 自助开通向导 13 项动作统一派发层（ADR-0358）。

将 ``/v1/dify/management/environment-wizard`` 的全部 13 个受控 ``wizard_action``
与底层 P0/P1 模块（主机双模式引导、自动发现、二次确认白名单修库、离线制品池、
基建编排、内置知识库上传、免 JSON 三能力自验、发布后公网 HTTPS 4 项冒烟）
统一收敛在一处无环薄派发层中，严格守卫：
- 显式目录授权（含单环境/全局范围边界 ``allowed``）；
- 每动作精确字段白名单（拒绝多余或未知键）；
- 零明文凭据持久化与零秘密回显；
- 宿主已钉扎指纹前提检查（未引导主机 fail-closed 拒绝）。
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    validate_private_file,
    write_private_text,
)
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.onboarding import (
    artifact_pool,
    db_remediation,
    discovery,
    host_enrollment,
    infra_orchestrator,
    public_smoke,
    registry,
    wizard_validation,
)

#: 必须与控制台桥 ``_WIZARD_ACTIONS`` 保持严格一致的 13 项动作白名单。
SUPPORTED_WIZARD_ACTIONS = frozenset(
    {
        "host_status",
        "host_enroll_password",
        "host_create_bootstrap_script",
        "host_revoke",
        "discover",
        "db_remediate",
        "artifact_pool_status",
        "infra_provision",
        "infra_status",
        "kb_upload",
        "kb_install_starter_faq",
        "self_validate",
        "post_publish_smoke",
    }
)

_INFRA_STATUS_SCHEMA = "aiops-infra-status-store/1"
#: Base64 编码后体积上限（10 MiB 原始文档的 4/3 倍余量），解码前即拦截防内存放大。
_MAX_KB_BASE64_CHARS = ((wizard_validation.MAX_KNOWLEDGE_DOC_BYTES + 2) // 3) * 4 + 64


@dataclass
class WizardRuntimeHooks:
    """可注入的执行钩子；生产侧由受控运行器绑定，测试侧可无网络确定性注入。"""

    host_bootstrap_runner: Callable[..., dict[str, Any]] | None = None
    host_keygen_runner: Callable[[Path, str], tuple[str, str]] | None = None
    discovery_runner: Callable[[dict[str, Any], str, str], tuple[dict[str, Any], dict[str, Any]]] | None = (
        None
    )
    db_executor: Callable[[str, tuple[str, ...]], None] | None = None
    db_reverify_runner: Callable[[str], dict[str, Any]] | None = None
    allowlist_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    node_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    nginx_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    live_probe: Callable[[str], dict[str, Any]] | None = None
    kb_uploader: Callable[[str, str, str, bytes], dict[str, Any]] | None = None
    validator_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    http_caller: Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, str]] | None = None


def _is_safe_env_id(value: Any) -> bool:
    if not isinstance(value, str) or not (1 <= len(value) <= 32):
        return False
    return (
        value[0].isalnum()
        and value[-1].isalnum()
        and all(ch.isascii() and (ch.islower() or ch.isdigit() or ch in "_-") for ch in value)
    )


def _require_env_access(allowed: set[str], env: Any) -> str:
    if not _is_safe_env_id(env):
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    if "*" not in allowed and env not in allowed:
        raise auth.SetupRefused("ENVIRONMENT_ACCESS_DENIED")
    return env


def _require_enrolled_host(
    registry_path: str | Path,
    identity: tuple[str, str, str],
    environment: str,
) -> dict[str, Any]:
    host_info = host_enrollment.get_host_status(registry_path, identity, environment)
    if not host_info.get("enrolled") or not isinstance(host_info.get("host"), dict):
        raise auth.SetupRefused("ENVIRONMENT_HOST_NOT_ENROLLED")
    return host_info["host"]


def _infra_status_path(registry_path: str | Path) -> Path:
    return Path(registry_path).parent / "infra-status.json"


def _load_infra_status_store(registry_path: str | Path) -> dict[str, Any]:
    store_path = _infra_status_path(registry_path)
    if not store_path.exists():
        return {"schema": _INFRA_STATUS_SCHEMA, "environments": {}}
    validate_private_file(store_path)
    body = json.loads(store_path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("environments"), dict):
        return {"schema": _INFRA_STATUS_SCHEMA, "environments": {}}
    return body


def _save_infra_status(registry_path: str | Path, environment: str, result: dict[str, Any]) -> None:
    store_path = _infra_status_path(registry_path)
    store = _load_infra_status_store(registry_path)
    store["environments"][environment] = {
        "environment": environment,
        "passed": bool(result.get("passed")),
        "failed_step": str(result.get("failed_step") or ""),
        "steps": list(result.get("steps") or []),
        "updated_at": time.time(),
    }
    ensure_private_directory(store_path.parent)
    write_private_text(store_path, json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _get_hooks(context: Any) -> WizardRuntimeHooks:
    hooks = getattr(context, "wizard_hooks", None)
    if isinstance(hooks, WizardRuntimeHooks):
        return hooks
    return WizardRuntimeHooks()


def _decode_kb_base64(raw_b64: Any) -> bytes:
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_CONTENT_EMPTY")
    compact = "".join(raw_b64.split())
    if len(compact) > _MAX_KB_BASE64_CHARS:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_FILE_TOO_LARGE")
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_CONTENT_INVALID") from exc
    if not decoded:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_CONTENT_EMPTY")
    if len(decoded) > wizard_validation.MAX_KNOWLEDGE_DOC_BYTES:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_FILE_TOO_LARGE")
    return decoded


def _dispatch_host_action(
    action: str,
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    if action == "host_status":
        if set(wp) != {"environment"}:
            raise host_enrollment.EnrollmentRefused("ENVIRONMENT_WIZARD_INVALID")
        return host_enrollment.get_host_status(path, identity, wp["environment"])
    if action == "host_revoke":
        if set(wp) != {"environment"}:
            raise host_enrollment.EnrollmentRefused("ENVIRONMENT_WIZARD_INVALID")
        return host_enrollment.revoke_host(path, identity, wp["environment"])
    if action == "host_enroll_password":
        allowed_keys = {"environment", "address", "password", "ssh_port", "ssh_user"}
        if not {"environment", "address", "password"}.issubset(wp) or set(wp) - allowed_keys:
            raise host_enrollment.EnrollmentRefused("ENVIRONMENT_WIZARD_INVALID")
        return host_enrollment.enroll_via_ephemeral_password(
            path,
            identity,
            environment=wp["environment"],
            address=wp["address"],
            password=wp["password"],
            ssh_port=wp.get("ssh_port", host_enrollment.DEFAULT_SSH_PORT),
            ssh_user=wp.get("ssh_user", host_enrollment.DEFAULT_SSH_USER),
            bootstrap_runner=hooks.host_bootstrap_runner,
            keygen_runner=hooks.host_keygen_runner,
        )
    allowed_keys = {
        "environment",
        "address",
        "callback_base_url",
        "ssh_port",
        "ssh_user",
        "ttl_seconds",
    }
    if not {"environment", "address", "callback_base_url"}.issubset(wp) or set(wp) - allowed_keys:
        raise host_enrollment.EnrollmentRefused("ENVIRONMENT_WIZARD_INVALID")
    return host_enrollment.create_bootstrap_script_session(
        path,
        identity,
        environment=wp["environment"],
        address=wp["address"],
        callback_base_url=wp["callback_base_url"],
        ssh_port=wp.get("ssh_port", host_enrollment.DEFAULT_SSH_PORT),
        ssh_user=wp.get("ssh_user", host_enrollment.DEFAULT_SSH_USER),
        ttl_seconds=wp.get("ttl_seconds", host_enrollment.BOOTSTRAP_TOKEN_TTL_SECONDS),
        keygen_runner=hooks.host_keygen_runner,
    )


def _dispatch_discover(
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    allowed_keys = {"environment", "api_domain", "internal_gateway_addr"}
    if not {"environment", "api_domain"}.issubset(wp) or set(wp) - allowed_keys:
        raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")
    env = wp["environment"]
    api_domain = wp["api_domain"]
    internal_gw = wp.get("internal_gateway_addr", "")
    if not isinstance(api_domain, str) or not isinstance(internal_gw, str):
        raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")
    # 校验输入域名格式并在执行前断言目标主机已完成指纹钉扎引导
    discovery.build_remote_discovery_probe_script(
        environment=env,
        api_domain=api_domain,
        internal_gateway_addr=internal_gw,
    )
    enrolled_host = _require_enrolled_host(path, identity, env)
    if hooks.discovery_runner is None:
        raise discovery.DiscoveryRefused("ENVIRONMENT_DISCOVERY_RUNNER_UNCONFIGURED")
    host_facts, data_preflight_report = hooks.discovery_runner(enrolled_host, api_domain, internal_gw)
    return discovery.parse_and_enrich_discovery_report(
        environment=env,
        host_facts=host_facts,
        data_preflight_report=data_preflight_report,
    )


def _dispatch_db_remediate(
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    required_keys = {
        "environment",
        "remediation_ids",
        "sql_statements",
        "confirmed_by_operator",
        "confirmation_digest",
    }
    if set(wp) != required_keys:
        raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")
    if (
        not isinstance(wp["remediation_ids"], list)
        or not isinstance(wp["sql_statements"], list)
        or type(wp["confirmed_by_operator"]) is not bool
        or not isinstance(wp["confirmation_digest"], str)
    ):
        raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")
    env = wp["environment"]
    _require_enrolled_host(path, identity, env)
    if hooks.db_executor is None or hooks.db_reverify_runner is None:
        raise db_remediation.RemediationRefused("ENVIRONMENT_DB_REMEDIATION_EXECUTOR_UNCONFIGURED")
    actor = ":".join(identity)
    audit_path = Path(path).parent / "db-remediation-audit.jsonl"
    return db_remediation.execute_confirmed_db_remediation(
        environment=env,
        actor=actor,
        remediation_ids=wp["remediation_ids"],
        sql_statements=wp["sql_statements"],
        confirmed_by_operator=wp["confirmed_by_operator"],
        confirmation_digest=wp["confirmation_digest"],
        executor=lambda stmts: hooks.db_executor(env, stmts),
        reverify_runner=lambda: hooks.db_reverify_runner(env),
        audit_log_path=audit_path,
    )


def _dispatch_infra(
    action: str,
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    if action == "infra_status":
        if set(wp) != {"environment"}:
            raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
        env = wp["environment"]
        store = _load_infra_status_store(path)
        entry = store["environments"].get(env)
        if not isinstance(entry, dict):
            return {
                "schema": "aiops-infra-provision/1",
                "environment": env,
                "provisioned": False,
                "passed": False,
                "failed_step": "",
                "steps": [],
            }
        return {
            "schema": "aiops-infra-provision/1",
            "environment": env,
            "provisioned": True,
            "passed": bool(entry.get("passed")),
            "failed_step": str(entry.get("failed_step") or ""),
            "steps": list(entry.get("steps") or []),
            "updated_at": entry.get("updated_at"),
        }

    allowed_keys = {"environment", "egress_ip", "api_domain", "gateway_url", "local_port", "allow_sources"}
    required_keys = {"environment", "egress_ip", "api_domain", "gateway_url"}
    if not required_keys.issubset(wp) or set(wp) - allowed_keys:
        raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
    allow_sources = wp.get("allow_sources", [])
    if not isinstance(allow_sources, list) or not all(isinstance(x, str) for x in allow_sources):
        raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
    env = wp["environment"]
    _require_enrolled_host(path, identity, env)
    result = infra_orchestrator.provision_environment_infra(
        env,
        egress_ip=wp["egress_ip"],
        api_domain=wp["api_domain"],
        gateway_url=wp["gateway_url"],
        local_port=wp.get("local_port", 8788),
        allow_sources=allow_sources,
        allowlist_runner=hooks.allowlist_runner,
        node_runner=hooks.node_runner,
        nginx_runner=hooks.nginx_runner,
        live_probe=hooks.live_probe,
    )
    _save_infra_status(path, env, result)
    return result


def _dispatch_kb(
    action: str,
    wp: dict[str, Any],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    if hooks.kb_uploader is None:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_KB_UPLOADER_UNCONFIGURED")
    if action == "kb_install_starter_faq":
        if set(wp) != {"environment", "kb_id"}:
            raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
        return wizard_validation.install_consumer_starter_faq(
            kb_id=wp["kb_id"],
            uploader=hooks.kb_uploader,
        )
    if set(wp) != {"environment", "target_role", "kb_id", "filename", "content_base64"}:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    content_bytes = _decode_kb_base64(wp["content_base64"])
    return wizard_validation.upload_wizard_knowledge_doc(
        target_role=wp["target_role"],
        kb_id=wp["kb_id"],
        filename=wp["filename"],
        content_bytes=content_bytes,
        uploader=hooks.kb_uploader,
    )


def _dispatch_self_validate(wp: dict[str, Any], hooks: WizardRuntimeHooks) -> dict[str, Any]:
    required_keys = {
        "environment",
        "tenant_id",
        "company_gateway_url",
        "order_no",
        "device_id",
        "offline_start",
        "offline_end",
        "consumer_order_no",
        "consumer_reference_id",
    }
    optional_keys = {"consumer_session", "consumer_phone", "consumer_code", "operator_token"}
    if not required_keys.issubset(wp) or set(wp) - (required_keys | optional_keys):
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    for req_key in required_keys:
        if not isinstance(wp[req_key], str):
            raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    for opt_key in optional_keys:
        if wp.get(opt_key) is not None and not isinstance(wp[opt_key], str):
            raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    if hooks.validator_runner is None:
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_VALIDATION_RUNNER_UNCONFIGURED")
    return wizard_validation.run_zero_json_self_validation(
        environment=wp["environment"],
        tenant_id=wp["tenant_id"],
        company_gateway_url=wp["company_gateway_url"],
        order_no=wp["order_no"],
        device_id=wp["device_id"],
        offline_start=wp["offline_start"],
        offline_end=wp["offline_end"],
        consumer_order_no=wp["consumer_order_no"],
        consumer_reference_id=wp["consumer_reference_id"],
        validator_runner=hooks.validator_runner,
        consumer_session=wp.get("consumer_session"),
        consumer_phone=wp.get("consumer_phone"),
        consumer_code=wp.get("consumer_code"),
        operator_token=wp.get("operator_token"),
        http_caller=hooks.http_caller,
    )


def _dispatch_post_publish_smoke(wp: dict[str, Any], hooks: WizardRuntimeHooks) -> dict[str, Any]:
    required_keys = {"environment", "gateway_url", "tenant_id"}
    optional_keys = {
        "consumer_session",
        "operator_token",
        "consumer_question",
        "operator_question",
    }
    if not required_keys.issubset(wp) or set(wp) - (required_keys | optional_keys):
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    for req_key in required_keys:
        if not isinstance(wp[req_key], str):
            raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    for opt_key in optional_keys:
        if wp.get(opt_key) is not None and not isinstance(wp[opt_key], str):
            raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    try:
        kwargs: dict[str, Any] = {
            "consumer_session": wp.get("consumer_session"),
            "operator_token": wp.get("operator_token"),
            "http_caller": hooks.http_caller,
        }
        if isinstance(wp.get("consumer_question"), str) and wp["consumer_question"].strip():
            kwargs["consumer_question"] = wp["consumer_question"]
        if isinstance(wp.get("operator_question"), str) and wp["operator_question"].strip():
            kwargs["operator_question"] = wp["operator_question"]
        return public_smoke.run_post_publish_https_smoke(
            wp["gateway_url"],
            wp["tenant_id"],
            **kwargs,
        )
    except ValueError as exc:
        raise auth.SetupRefused("ENVIRONMENT_SMOKE_INVALID") from exc


def dispatch_wizard(
    context: Any,
    session: Any,
    payload: Any,
    *,
    access_fn: Callable[[Any, Any], tuple[Any, dict[str, Any], set[str]]],
    path_fn: Callable[[Any], str],
    identity_fn: Callable[[Any, Any], tuple[str, str, str]],
) -> dict[str, Any]:
    """V2 自助开通向导统一入口（ADR-0358）：覆盖全部 13 项白名单动作。"""
    caller, _, allowed = access_fn(context, session)
    if not isinstance(payload, dict) or set(payload) - {"wizard_action", "wizard_payload"}:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    action = payload.get("wizard_action")
    wp = payload.get("wizard_payload")
    if not isinstance(action, str) or not isinstance(wp, dict):
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    if action not in SUPPORTED_WIZARD_ACTIONS:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED")

    _require_env_access(allowed, wp.get("environment"))
    path = path_fn(context)
    identity = identity_fn(context, caller)
    hooks = _get_hooks(context)

    try:
        if action in {
            "host_status",
            "host_revoke",
            "host_enroll_password",
            "host_create_bootstrap_script",
        }:
            return _dispatch_host_action(action, wp, path, identity, hooks)
        if action == "discover":
            return _dispatch_discover(wp, path, identity, hooks)
        if action == "db_remediate":
            return _dispatch_db_remediate(wp, path, identity, hooks)
        if action == "artifact_pool_status":
            if set(wp) != {"environment"}:
                raise artifact_pool.ArtifactPoolRefused("ENVIRONMENT_WIZARD_INVALID")
            return artifact_pool.list_pool_status(Path(path).parent / "artifact-pool.json")
        if action in {"infra_provision", "infra_status"}:
            return _dispatch_infra(action, wp, path, identity, hooks)
        if action in {"kb_upload", "kb_install_starter_faq"}:
            return _dispatch_kb(action, wp, hooks)
        if action == "self_validate":
            return _dispatch_self_validate(wp, hooks)
        if action == "post_publish_smoke":
            return _dispatch_post_publish_smoke(wp, hooks)
    except host_enrollment.EnrollmentRefused as exc:
        if str(exc) == "ENVIRONMENT_HOST_FINGERPRINT_MISMATCH":
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except (
        discovery.DiscoveryRefused,
        db_remediation.RemediationRefused,
        artifact_pool.ArtifactPoolRefused,
        infra_orchestrator.InfraProvisionRefused,
        wizard_validation.WizardValidationRefused,
    ) as exc:
        raise auth.SetupRefused(str(exc)) from exc
    except auth.SetupRefused:
        raise
    except (registry.RegistryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_UNAVAILABLE") from exc
    raise auth.SetupRefused("ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED")
