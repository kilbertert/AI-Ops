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
    wizard_jobs,
    wizard_runtime,
    wizard_validation,
)

#: 必须与控制台桥 ``_WIZARD_ACTIONS`` 保持严格一致的 14 项动作白名单。
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
        "publish_environment",
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
    publish_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
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
    optional_keys = {
        "internal_gateway_addr",
        "mysql_host",
        "mysql_port",
        "mysql_user",
        "mysql_password",
        "redis_host",
        "redis_port",
        "redis_password",
    }
    allowed_keys = {"environment", "api_domain"} | optional_keys
    if not {"environment", "api_domain"}.issubset(wp) or set(wp) - allowed_keys:
        raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")
    env = wp["environment"]
    api_domain = wp["api_domain"]
    internal_gw = wp.get("internal_gateway_addr", "")
    if not isinstance(api_domain, str) or not isinstance(internal_gw, str):
        raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")
    for str_key in ("mysql_host", "mysql_user", "mysql_password", "redis_host", "redis_password"):
        if wp.get(str_key) is not None and not isinstance(wp[str_key], str):
            raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")
    mysql_port = wp.get("mysql_port", 3306)
    redis_port = wp.get("redis_port", 6379)
    if (
        type(mysql_port) is not int
        or not (1 <= mysql_port <= 65535)
        or type(redis_port) is not int
        or not (1 <= redis_port <= 65535)
    ):
        raise discovery.DiscoveryRefused("ENVIRONMENT_WIZARD_INVALID")

    # 校验输入域名格式并在执行前断言目标主机已完成指纹钉扎引导
    discovery.build_remote_discovery_probe_script(
        environment=env,
        api_domain=api_domain,
        internal_gateway_addr=internal_gw,
    )
    enrolled_host = _require_enrolled_host(path, identity, env)
    if hooks.discovery_runner is not None:
        host_facts, data_preflight_report = hooks.discovery_runner(enrolled_host, api_domain, internal_gw)
        return discovery.parse_and_enrich_discovery_report(
            environment=env,
            host_facts=host_facts,
            data_preflight_report=data_preflight_report,
        )
    return wizard_runtime.run_live_discovery(
        path,
        environment=env,
        api_domain=api_domain,
        internal_gateway_addr=internal_gw,
        mysql_host=wp.get("mysql_host") or "",
        mysql_port=mysql_port,
        mysql_user=wp.get("mysql_user") or "aiops_ro",
        mysql_password=wp.get("mysql_password") or "",
        redis_host=wp.get("redis_host") or "",
        redis_port=redis_port,
        redis_password=wp.get("redis_password") or "",
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
    optional_keys = {
        "admin_mysql_host",
        "admin_mysql_port",
        "admin_mysql_user",
        "admin_mysql_password",
        "mysql_host",
        "mysql_port",
        "mysql_user",
        "mysql_password",
    }
    if not required_keys.issubset(wp) or set(wp) - (required_keys | optional_keys):
        raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")
    if (
        not isinstance(wp["remediation_ids"], list)
        or not isinstance(wp["sql_statements"], list)
        or type(wp["confirmed_by_operator"]) is not bool
        or not isinstance(wp["confirmation_digest"], str)
    ):
        raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")
    for str_key in (
        "admin_mysql_host",
        "admin_mysql_user",
        "admin_mysql_password",
        "mysql_host",
        "mysql_user",
        "mysql_password",
    ):
        if wp.get(str_key) is not None and not isinstance(wp[str_key], str):
            raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")
    admin_port = wp.get("admin_mysql_port", 0)
    ro_port = wp.get("mysql_port", 3306)
    if (
        type(admin_port) is not int
        or not (0 <= admin_port <= 65535)
        or type(ro_port) is not int
        or not (1 <= ro_port <= 65535)
    ):
        raise db_remediation.RemediationRefused("ENVIRONMENT_WIZARD_INVALID")

    env = wp["environment"]
    _require_enrolled_host(path, identity, env)
    actor = ":".join(identity)
    audit_path = Path(path).parent / "db-remediation-audit.jsonl"
    if hooks.db_executor is not None and hooks.db_reverify_runner is not None:
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
    return wizard_runtime.run_live_db_remediation(
        path,
        environment=env,
        actor=actor,
        remediation_ids=wp["remediation_ids"],
        sql_statements=wp["sql_statements"],
        confirmed_by_operator=wp["confirmed_by_operator"],
        confirmation_digest=wp["confirmation_digest"],
        admin_mysql_host=wp.get("admin_mysql_host") or "",
        admin_mysql_port=admin_port,
        admin_mysql_user=wp.get("admin_mysql_user") or "",
        admin_mysql_password=wp.get("admin_mysql_password") or "",
        mysql_host=wp.get("mysql_host") or "",
        mysql_port=ro_port,
        mysql_user=wp.get("mysql_user") or "aiops_ro",
        mysql_password=wp.get("mysql_password") or "",
    )


def _dispatch_infra(
    action: str,
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    if action == "infra_status":
        allowed_status_keys = {"environment", "action", "job_id"}
        if "environment" not in wp or set(wp) - allowed_status_keys:
            raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
        env = wp["environment"]
        job_id = wp.get("job_id")
        target_action = str(wp.get("action") or "infra_provision")
        if target_action not in {"infra_provision", "publish_environment"}:
            raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
        if job_id is not None:
            if not isinstance(job_id, str):
                raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
            job = wizard_jobs.get_job(path, job_id)
            if job is None or job["environment"] != env:
                raise wizard_jobs.WizardJobError("ENVIRONMENT_WIZARD_JOB_NOT_FOUND")
            return job
        latest_job = wizard_jobs.get_latest_job(path, environment=env, action=target_action)
        if latest_job is not None:
            return latest_job
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

    allowed_keys = {
        "environment",
        "egress_ip",
        "api_domain",
        "gateway_url",
        "local_port",
        "allow_sources",
        "tenant_id",
        "tenant_name",
        "customer_name",
        "python_minor",
        "gateway_internal_addr",
    }
    required_keys = {"environment", "egress_ip", "api_domain", "gateway_url"}
    if not required_keys.issubset(wp) or set(wp) - allowed_keys:
        raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
    allow_sources = wp.get("allow_sources", [])
    if not isinstance(allow_sources, list) or not all(isinstance(x, str) for x in allow_sources):
        raise infra_orchestrator.InfraProvisionRefused("ENVIRONMENT_WIZARD_INVALID")
    env = wp["environment"]
    enrolled = _require_enrolled_host(path, identity, env)

    # 先行做完整输入契约校验；非法 IP / 域名 / 非 HTTPS 地址立即 400 拒绝，绝不入队。
    allow_plan = infra_orchestrator.plan_egress_allowlist(env, wp["egress_ip"])
    node_plan = infra_orchestrator.plan_execution_node(env, gateway_url=wp["gateway_url"])
    nginx_plan = infra_orchestrator.render_customer_nginx_extensions(
        env,
        api_domain=wp["api_domain"],
        local_port=wp.get("local_port", 8788),
        allow_sources=allow_sources,
    )

    if any(
        runner is not None
        for runner in (hooks.allowlist_runner, hooks.node_runner, hooks.nginx_runner, hooks.live_probe)
    ):
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

    job_req = {
        "environment": env,
        "egress_ip": allow_plan["egress_ip"],
        "api_domain": nginx_plan["api_domain"],
        "gateway_url": node_plan["gateway_url"],
        "local_port": nginx_plan["local_port"],
        "allow_sources": allow_sources,
        "tenant_id": str(wp.get("tenant_id") or "").strip(),
        "tenant_name": str(wp.get("tenant_name") or "").strip(),
        "customer_name": str(wp.get("customer_name") or enrolled.get("customer_name") or env).strip(),
        "python_minor": str(wp.get("python_minor") or "3.12").strip() or "3.12",
        "gateway_internal_addr": str(
            wp.get("gateway_internal_addr") or enrolled.get("gateway_internal_addr") or ""
        ).strip(),
        "actor": identity[2],
    }
    return wizard_jobs.enqueue_job(path, environment=env, action="infra_provision", request=job_req)


def _dispatch_kb(
    action: str,
    wp: dict[str, Any],
    context: Any,
    path: str | Path,
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    tenant_id = str(wp.get("tenant_id") or "").strip()
    if wp.get("tenant_id") is not None and not isinstance(wp["tenant_id"], str):
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    uploader = hooks.kb_uploader
    if uploader is None:
        settings = getattr(context, "settings", None)

        def uploader(role: str, target_kb_id: str, fname: str, raw: bytes) -> dict[str, Any]:
            return wizard_runtime.upload_to_kb_service(
                settings,
                path,
                environment=wp["environment"],
                tenant_id=tenant_id,
                target_role=role,
                kb_id=target_kb_id,
                filename=fname,
                content_bytes=raw,
            )

    if action == "kb_install_starter_faq":
        if not {"environment", "kb_id"}.issubset(wp) or set(wp) - {"environment", "kb_id", "tenant_id"}:
            raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
        return wizard_validation.install_consumer_starter_faq(
            kb_id=wp["kb_id"],
            uploader=uploader,
        )
    req_upload = {"environment", "target_role", "kb_id", "filename", "content_base64"}
    if not req_upload.issubset(wp) or set(wp) - (req_upload | {"tenant_id"}):
        raise wizard_validation.WizardValidationRefused("ENVIRONMENT_WIZARD_INVALID")
    content_bytes = _decode_kb_base64(wp["content_base64"])
    return wizard_validation.upload_wizard_knowledge_doc(
        target_role=wp["target_role"],
        kb_id=wp["kb_id"],
        filename=wp["filename"],
        content_bytes=content_bytes,
        uploader=uploader,
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
    runner = hooks.validator_runner
    if runner is None:
        base_gw = wp["company_gateway_url"]

        def runner(cases_payload: dict[str, Any]) -> dict[str, Any]:
            enriched = {**cases_payload, "company_gateway_url": base_gw}
            return wizard_runtime.run_live_self_validation(
                enriched,
                http_caller=hooks.http_caller,
            )

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
        validator_runner=runner,
        consumer_session=wp.get("consumer_session"),
        consumer_phone=wp.get("consumer_phone"),
        consumer_code=wp.get("consumer_code"),
        operator_token=wp.get("operator_token"),
        http_caller=hooks.http_caller,
    )


def _dispatch_publish_environment(
    wp: dict[str, Any],
    path: str | Path,
    identity: tuple[str, str, str],
    hooks: WizardRuntimeHooks,
) -> dict[str, Any]:
    allowed_keys = {"environment", "tenant_id", "tenant_name", "gateway_url", "customer_name", "job_id"}
    if "environment" not in wp or set(wp) - allowed_keys:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    env = str(wp["environment"])
    if isinstance(wp.get("job_id"), str) and wp["job_id"].strip():
        job = wizard_jobs.get_job(path, wp["job_id"].strip())
        if job is None or job["environment"] != env:
            raise wizard_jobs.WizardJobError("ENVIRONMENT_WIZARD_JOB_NOT_FOUND")
        return job

    tenant_id = str(wp.get("tenant_id") or "").strip()
    if not tenant_id:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_INVALID")
    enrolled = _require_enrolled_host(path, identity, env)
    gateway_url = str(wp.get("gateway_url") or "").strip()
    if gateway_url:
        infra_orchestrator._validate_https_url(gateway_url)
    elif enrolled.get("api_domain"):
        gateway_url = f"https://{enrolled['api_domain']}"
    req_payload = {
        "environment": env,
        "tenant_id": tenant_id,
        "tenant_name": str(wp.get("tenant_name") or tenant_id).strip(),
        "customer_name": str(wp.get("customer_name") or enrolled.get("customer_name") or env).strip(),
        "gateway_url": gateway_url,
        "actor": identity[2],
    }
    if hooks.publish_runner is not None:
        return hooks.publish_runner(req_payload)
    return wizard_jobs.enqueue_job(path, environment=env, action="publish_environment", request=req_payload)


def _dispatch_post_publish_smoke(wp: dict[str, Any], hooks: WizardRuntimeHooks) -> dict[str, Any]:
    required_keys = {"environment", "gateway_url", "tenant_id"}
    optional_keys = {
        "consumer_session",
        "consumer_phone",
        "consumer_code",
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

    gateway_url = wp["gateway_url"]
    tenant_id = wp["tenant_id"]
    ephemeral_consumer = str(wp.get("consumer_session") or "").strip()
    c_phone = str(wp.get("consumer_phone") or "").strip()
    c_code = str(wp.get("consumer_code") or "").strip()
    op_tok = str(wp.get("operator_token") or "").strip()
    revoked: bool | None = None

    try:
        if not ephemeral_consumer and c_phone and c_code:
            ephemeral_consumer = wizard_validation.exchange_ephemeral_consumer_session(
                company_gateway_url=gateway_url,
                phone=c_phone,
                code=c_code,
                tenant_id=tenant_id,
                http_caller=hooks.http_caller,
            )
        kwargs: dict[str, Any] = {
            "consumer_session": ephemeral_consumer or None,
            "operator_token": op_tok or None,
            "http_caller": hooks.http_caller,
        }
        if isinstance(wp.get("consumer_question"), str) and wp["consumer_question"].strip():
            kwargs["consumer_question"] = wp["consumer_question"]
        if isinstance(wp.get("operator_question"), str) and wp["operator_question"].strip():
            kwargs["operator_question"] = wp["operator_question"]
        try:
            res = public_smoke.run_post_publish_https_smoke(
                gateway_url,
                tenant_id,
                **kwargs,
            )
        finally:
            if op_tok:
                try:
                    revoked = wizard_validation.revoke_ephemeral_operator_token(
                        company_gateway_url=gateway_url,
                        operator_token=op_tok,
                        http_caller=hooks.http_caller,
                    )
                except Exception:
                    revoked = False
        if revoked is not None:
            res["operator_session_revoked"] = revoked
        return res
    except wizard_validation.WizardValidationRefused:
        raise
    except ValueError as exc:
        raise auth.SetupRefused("ENVIRONMENT_SMOKE_INVALID") from exc
    finally:
        ephemeral_consumer = ""
        c_phone = ""
        c_code = ""
        op_tok = ""


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
            assets_pool = Path(path).resolve().parent.parent / "onboarding-assets" / "artifact-pool.json"
            pool_path = assets_pool if assets_pool.is_file() else (Path(path).parent / "artifact-pool.json")
            return artifact_pool.list_pool_status(pool_path)
        if action in {"infra_provision", "infra_status"}:
            return _dispatch_infra(action, wp, path, identity, hooks)
        if action in {"kb_upload", "kb_install_starter_faq"}:
            return _dispatch_kb(action, wp, context, path, hooks)
        if action == "self_validate":
            return _dispatch_self_validate(wp, hooks)
        if action == "publish_environment":
            return _dispatch_publish_environment(wp, path, identity, hooks)
        if action == "post_publish_smoke":
            return _dispatch_post_publish_smoke(wp, hooks)
    except host_enrollment.EnrollmentRefused as exc:
        if str(exc) == "ENVIRONMENT_HOST_FINGERPRINT_MISMATCH":
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except wizard_jobs.WizardJobError as exc:
        raise auth.SetupRefused(str(exc)) from exc
    except (
        discovery.DiscoveryRefused,
        db_remediation.RemediationRefused,
        artifact_pool.ArtifactPoolRefused,
        infra_orchestrator.InfraProvisionRefused,
        wizard_validation.WizardValidationRefused,
        wizard_runtime.WizardRuntimeError,
    ) as exc:
        raise auth.SetupRefused(str(exc)) from exc
    except auth.SetupRefused:
        raise
    except (registry.RegistryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_WIZARD_UNAVAILABLE") from exc
    raise auth.SetupRefused("ENVIRONMENT_WIZARD_ACTION_UNSUPPORTED")
