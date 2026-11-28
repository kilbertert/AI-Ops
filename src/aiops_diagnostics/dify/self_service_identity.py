"""只用正常Dify登录交换的公司令牌查询管理身份；浏览器不提供身份声明。"""

from dataclasses import dataclass, field

from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.common.config import Settings


class SetupRefused(ValueError):
    pass


@dataclass(frozen=True)
class ManagerIdentity:
    user_id: str
    tenant_id: str
    platform: bool
    token: str = field(repr=False)
    environment: str = ""
    username: str = ""


def _upms_base_url(context, environment: str = "") -> str:
    control_env = str(getattr(getattr(context, "settings", None), "environment", "") or "")
    if environment and environment != control_env:
        reg_path = management_registry_path(context)
        if not reg_path:
            raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
        from aiops_diagnostics.onboarding.registry import EnvironmentRegistry, RegistryError

        try:
            entry = EnvironmentRegistry(reg_path, environment).entry()
        except RegistryError as exc:
            raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE") from exc
        company_gw = str(entry.get("company_gateway_url") or "").strip()
        if not company_gw:
            raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
        return company_gw.rstrip("/") + "/upms"
    base = Settings.from_config(context.settings.server_config_file).upms.base_url
    if not base:
        raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
    return base


def company_get(context, token, path, *, environment: str = ""):
    base = _upms_base_url(context, environment=environment)

    def unavailable(_):
        return SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")

    body = request_json(
        RequestSpec(
            url=base.rstrip("/") + path,
            headers={"Authorization": "Bearer " + token, "client-type": "admin"},
            timeout=10,
            max_read_bytes=1048576,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=unavailable,
            http_error=unavailable,
            unavailable=unavailable,
            invalid_body=unavailable,
            invalid_envelope=unavailable,
        ),
    )
    if not isinstance(body, dict) or body.get("code") != 0 or "data" not in body:
        raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
    return body["data"]


def identity(context, session, *, require_admission=True, require_platform_permissions=True):
    exchanged = context.operator_cpms.console_identity(session)
    control_env = str(getattr(getattr(context, "settings", None), "environment", "") or "")
    session_env = str(exchanged.get("environment") or control_env)
    info = (
        company_get(context, exchanged["token"], "/user/info", environment=session_env)
        if session_env and session_env != control_env
        else company_get(context, exchanged["token"], "/user/info")
    )
    user = info.get("sysUser", {}) if isinstance(info, dict) else {}
    uid, tenant = str(user.get("id") or ""), str(user.get("tenantId") or "")
    username = str(user.get("username") or "")
    if not uid or not tenant or uid != exchanged["user_id"] or tenant != exchanged["tenant_id"]:
        raise SetupRefused("SETUP_IDENTITY_MISMATCH")
    is_platform = str(user.get("type")) == "-1"
    registry_file = getattr(getattr(context, "settings", None), "environment_registry_file", "")
    managed_reg = management_registry_path(context)
    if require_admission and (registry_file or (session_env and session_env != control_env and managed_reg)):
        from aiops_diagnostics.onboarding.registry import EnvironmentRegistry, RegistryError, for_settings

        try:
            reg = (
                EnvironmentRegistry(managed_reg, session_env)
                if session_env and session_env != control_env
                else for_settings(context.settings)
            )
            if is_platform and tenant == "-1":
                reg.require_enabled()
            else:
                reg.require(tenant)
        except RegistryError as exc:
            raise SetupRefused("TENANT_ENVIRONMENT_NOT_ENABLED") from exc
    system_customer_override = bool(
        session_env and session_env != control_env and is_platform and tenant == "-1" and username == "system"
    )
    if (
        require_platform_permissions
        and is_platform
        and not system_customer_override
        and not {"aiops:agent:edit", "aiops:agent:publish"}.issubset(info.get("permissions") or [])
    ):
        raise SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    return ManagerIdentity(
        uid, tenant, is_platform, exchanged["token"], environment=session_env, username=username
    )


def tenants(context, caller):
    if not caller.platform:
        raise SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    control_env = str(getattr(getattr(context, "settings", None), "environment", "") or "")
    caller_env = caller.environment or control_env
    data = (
        company_get(context, caller.token, "/tenant/list/my", environment=caller_env)
        if caller_env and caller_env != control_env
        else company_get(context, caller.token, "/tenant/list/my")
    )
    if not isinstance(data, list) or len(data) > 2000:
        raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
    result = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
        tid = str(row.get("id") or "")
        if tid and tid != "-1":
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
                raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")
            result[tid] = {"id": tid, "name": row["name"][:128]}
    return list(result.values())


def require_target(context, caller, tenant_id):
    if not caller.platform:
        raise SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    found = next((t for t in tenants(context, caller) if t["id"] == tenant_id), None)
    if found is None:
        raise SetupRefused("SETUP_TENANT_FORBIDDEN")
    control_env = str(getattr(getattr(context, "settings", None), "environment", "") or "")
    caller_env = caller.environment or control_env
    registry_file = getattr(getattr(context, "settings", None), "environment_registry_file", "")
    managed_reg = management_registry_path(context)
    if registry_file or (caller_env and caller_env != control_env and managed_reg):
        from aiops_diagnostics.onboarding.registry import EnvironmentRegistry, RegistryError, for_settings

        try:
            reg = (
                EnvironmentRegistry(managed_reg, caller_env)
                if caller_env and caller_env != control_env
                else for_settings(context.settings)
            )
            reg.require(tenant_id)
        except RegistryError as exc:
            raise SetupRefused("TENANT_ENVIRONMENT_NOT_ENABLED") from exc
    return found


def management_registry_path(context) -> str:
    settings = getattr(context, "settings", None)
    return str(
        getattr(settings, "environment_management_registry_file", "")
        or getattr(settings, "environment_registry_file", "")
        or ""
    ).strip()


def manager_allowed_environments(context, caller: ManagerIdentity) -> set[str]:
    """返回发起人在控制面环境目录中被显式授权管理的环境集合（非显式 manager 返回空集）。"""
    path = management_registry_path(context)
    if not path:
        return set()
    from aiops_diagnostics.onboarding import registry as env_registry

    try:
        doc = env_registry.load(path)
    except Exception:  # noqa: BLE001 - 目录异常一律 fail-closed
        return set()
    control_env = str(getattr(context.settings, "environment", "") or "")
    allowed: set[str] = set()
    for row in doc.get("managers") or []:
        if (
            isinstance(row, dict)
            and row.get("environment") == control_env
            and row.get("tenant_id") == caller.tenant_id
            and row.get("user_id") == caller.user_id
        ):
            for env_id in row.get("environments") or []:
                if isinstance(env_id, str) and env_id:
                    allowed.add(env_id)
    return allowed


def require_environment_manager(context, caller: ManagerIdentity, environment: str) -> dict:
    """校验发起人是否有权跨环境管理目标环境，并返回该环境在控制面目录中的登记行。"""
    allowed = manager_allowed_environments(context, caller)
    if "*" not in allowed and environment not in allowed:
        raise SetupRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    path = management_registry_path(context)
    if not path:
        raise SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE")
    from aiops_diagnostics.onboarding import registry as env_registry

    try:
        doc = env_registry.load(path)
    except Exception as exc:  # noqa: BLE001
        raise SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE") from exc
    entry = next(
        (
            row
            for row in (doc.get("environments") or [])
            if isinstance(row, dict) and row.get("id") == environment
        ),
        None,
    )
    if entry is None:
        raise SetupRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    return entry


def environment_display(context, environment: str, tenant_id: str) -> tuple[str, str]:
    """查询环境名与租户名用于 Dify 发布条防呆展示；读不到时平稳退回原编号。"""
    path = management_registry_path(context)
    if not path:
        return environment, tenant_id
    from aiops_diagnostics.onboarding import registry as env_registry

    try:
        doc = env_registry.load(path)
    except Exception:  # noqa: BLE001
        return environment, tenant_id
    for row in doc.get("environments") or []:
        if isinstance(row, dict) and row.get("id") == environment:
            env_name = str(row.get("name") or environment)
            tenant_name = next(
                (
                    str(t.get("name") or tenant_id)
                    for t in (row.get("tenants") or [])
                    if isinstance(t, dict) and str(t.get("id") or "") == tenant_id
                ),
                tenant_id,
            )
            return env_name, tenant_name
    return environment, tenant_id
