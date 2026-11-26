"""客户环境外围基建幂等编排原语（ADR-0358 P1-3）。

将原本游离在编排之外的 4 项手工基建正式收编为幂等、可回滚、fail-closed 步骤：
1. ``allowlist``：同步 36 Dify API 与 41 执行入口的客户出口 IP 白名单；
2. ``execution_node``：在 41 拉起专属共享执行节点
   ``aiops-environment-node@<env>.service``（零宿主机暴露端口）；
3. ``customer_nginx``：在客户机渲染并安装工具入口 + 正式用户入口两份 Nginx 扩展
   （严格方法白名单 + ``0600`` 鉴权片段 + ``nginx -t`` 回滚门）；
4. ``live_probe``：安装后即刻验证匿名 ``POST /v1/assistant/questions`` 返回 ``401``，
   且客户既有 ``GET /upms/tenant/list`` 返回 ``200``。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit


class InfraProvisionRefused(ValueError):
    """基建编排输入校验失败或安全闸门拒绝时的稳定异常。"""


def _validate_environment(environment: str) -> str:
    if (
        not isinstance(environment, str)
        or not (1 <= len(environment) <= 32)
        or not (environment[0].isalnum() and environment[-1].isalnum())
        or not all(ch.isascii() and (ch.islower() or ch.isdigit() or ch in "_-") for ch in environment)
    ):
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_ENVIRONMENT")
    return environment


def _validate_ip(address: str, code: str = "ENVIRONMENT_INFRA_INVALID_IP") -> str:
    if not isinstance(address, str):
        raise InfraProvisionRefused(code)
    raw = address.strip()
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise InfraProvisionRefused(code) from exc
    return str(parsed)


def _validate_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_DOMAIN")
    raw = domain.strip().lower()
    if not raw or len(raw) > 253 or "." not in raw:
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_DOMAIN")
    labels = raw.split(".")
    for label in labels:
        if (
            not (1 <= len(label) <= 63)
            or not (label[0].isalnum() and label[-1].isalnum())
            or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label)
        ):
            raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_DOMAIN")
    return raw


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str):
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_HTTPS_REQUIRED")
    raw = url.strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_HTTPS_REQUIRED")
    return raw


def plan_egress_allowlist(environment: str, egress_ip: str) -> dict[str, Any]:
    """生成 36/41 出口 IP 白名单同步意图（仅允许合法 IP，防配置注入）。"""
    env = _validate_environment(environment)
    ip_str = _validate_ip(egress_ip)
    return {
        "step": "allowlist",
        "environment": env,
        "egress_ip": ip_str,
        "targets": ["36-dify-service-api", "41-execution-ingress"],
        "idempotent": True,
    }


def plan_execution_node(
    environment: str,
    *,
    gateway_url: str,
    model_id: int = 3,
) -> dict[str, Any]:
    """生成 41 侧专属共享执行节点（零宿主机暴露端口）部署规格。"""
    env = _validate_environment(environment)
    gw = _validate_https_url(gateway_url)
    if type(model_id) is not int or model_id < 1:
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_MODEL_ID")
    return {
        "step": "execution_node",
        "environment": env,
        "node_id": f"node-{env}",
        "unit_name": f"aiops-environment-node@{env}.service",
        "gateway_url": gw,
        "chat_model_id": model_id,
        "host_ports_exposed": 0,
    }


def render_customer_nginx_extensions(
    environment: str,
    *,
    api_domain: str,
    local_port: int = 8788,
    allow_sources: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """渲染客户机工具入口与正式用户入口两份 Nginx 扩展（严格方法白名单 + 0600 鉴权片段）。"""
    env = _validate_environment(environment)
    domain = _validate_domain(api_domain)
    if type(local_port) is not int or not (1024 <= local_port <= 65535):
        raise InfraProvisionRefused("ENVIRONMENT_INFRA_INVALID_PORT")
    validated_sources = [_validate_ip(src, "ENVIRONMENT_INFRA_INVALID_SOURCE_IP") for src in allow_sources]
    allow_block = (
        "\n".join(f"    allow {src};" for src in validated_sources) + "\n    deny all;"
        if validated_sources
        else "    deny all;"
    )
    tool_conf = "\n".join(
        [
            f"# AI-Ops managed tool ingress for {env} ({domain})",
            "location ^~ /_aiops_internal/ {",
            allow_block,
            "    if ($request_method !~ ^(GET|POST)$) { return 405; }",
            f"    proxy_pass http://127.0.0.1:{local_port};",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "}",
            "",
        ]
    )
    user_conf = "\n".join(
        [
            f"# AI-Ops managed formal user ingress for {env} ({domain})",
            f"include /etc/aiops-access-{env}/nginx-user-ingress-auth.conf;",
            "location = /v1/assistant/questions {",
            "    if ($request_method !~ ^(GET|POST)$) { return 405; }",
            f"    proxy_pass http://127.0.0.1:{local_port};",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "    proxy_set_header X-Forwarded-Proto https;",
            "}",
            "location ^~ /v1/assistant/questions/ {",
            "    if ($request_method !~ ^(GET|POST)$) { return 405; }",
            f"    proxy_pass http://127.0.0.1:{local_port};",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "    proxy_set_header X-Forwarded-Proto https;",
            "    proxy_buffering off;",
            "}",
            "",
        ]
    )
    return {
        "step": "customer_nginx",
        "environment": env,
        "api_domain": domain,
        "local_port": local_port,
        "files": {
            "aiops-tool-ingress.conf": tool_conf,
            "aiops-user-ingress.conf": user_conf,
        },
        "auth_snippet_path": f"/etc/aiops-access-{env}/nginx-user-ingress-auth.conf",
        "auth_snippet_mode": "0600",
        "requires_nginx_test_before_reload": True,
        "rollback_on_test_failure": True,
    }


def provision_environment_infra(
    environment: str,
    *,
    egress_ip: str,
    api_domain: str,
    gateway_url: str,
    local_port: int = 8788,
    allow_sources: tuple[str, ...] | list[str] = (),
    allowlist_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    node_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    nginx_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    live_probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """顺序执行 allowlist -> execution_node -> customer_nginx -> live_probe，任一步失败即停并标回滚。"""
    allow_plan = plan_egress_allowlist(environment, egress_ip)
    node_plan = plan_execution_node(environment, gateway_url=gateway_url)
    nginx_plan = render_customer_nginx_extensions(
        environment,
        api_domain=api_domain,
        local_port=local_port,
        allow_sources=allow_sources,
    )
    steps: list[dict[str, Any]] = []

    def _run_step(
        name: str, plan: dict[str, Any], runner: Callable[[dict[str, Any]], dict[str, Any]] | None
    ) -> bool:
        if runner is None:
            steps.append({"step": name, "state": "planned", "rollback_required": False})
            return True
        res = runner(plan)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        steps.append(
            {
                "step": name,
                "state": "passed" if ok else "failed",
                "rollback_required": not ok and bool(res.get("rolled_back", False) is False),
                "rolled_back": bool(res.get("rolled_back", False)),
                "reason": str(res.get("reason") or ""),
            }
        )
        return ok

    for name, plan, runner in (
        ("allowlist", allow_plan, allowlist_runner),
        ("execution_node", node_plan, node_runner),
        ("customer_nginx", nginx_plan, nginx_runner),
    ):
        if not _run_step(name, plan, runner):
            return {
                "schema": "aiops-infra-provision/1",
                "environment": allow_plan["environment"],
                "passed": False,
                "failed_step": name,
                "steps": steps,
            }

    if live_probe is not None:
        probe_res = live_probe(node_plan["gateway_url"])
        anon_st = int(probe_res.get("anon_assistant_status") or 0)
        upms_st = int(probe_res.get("upms_tenant_list_status") or 0)
        probe_ok = anon_st == 401 and upms_st == 200
        steps.append(
            {
                "step": "live_probe",
                "state": "passed" if probe_ok else "failed",
                "anon_assistant_status": anon_st,
                "upms_tenant_list_status": upms_st,
                "reason": "" if probe_ok else "LIVE_PROBE_CONTRACT_MISMATCH",
            }
        )
        if not probe_ok:
            return {
                "schema": "aiops-infra-provision/1",
                "environment": allow_plan["environment"],
                "passed": False,
                "failed_step": "live_probe",
                "steps": steps,
            }
    else:
        steps.append({"step": "live_probe", "state": "skipped"})

    return {
        "schema": "aiops-infra-provision/1",
        "environment": allow_plan["environment"],
        "passed": True,
        "failed_step": "",
        "steps": steps,
    }
