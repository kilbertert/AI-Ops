"""只用正常Dify登录交换的公司令牌查询管理身份；浏览器不提供身份声明。"""

from dataclasses import dataclass, field

from aiops_diagnostics.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.config import Settings


class SetupRefused(ValueError):
    pass


@dataclass(frozen=True)
class ManagerIdentity:
    user_id: str
    tenant_id: str
    platform: bool
    token: str = field(repr=False)


def company_get(context, token, path):
    base = Settings.from_config(context.settings.server_config_file).upms.base_url
    if not base:
        raise SetupRefused("COMPANY_DIRECTORY_UNAVAILABLE")

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


def identity(context, session):
    exchanged = context.operator_cpms.console_identity(session)
    info = company_get(context, exchanged["token"], "/user/info")
    user = info.get("sysUser", {}) if isinstance(info, dict) else {}
    uid, tenant = str(user.get("id") or ""), str(user.get("tenantId") or "")
    if not uid or not tenant or uid != exchanged["user_id"] or tenant != exchanged["tenant_id"]:
        raise SetupRefused("SETUP_IDENTITY_MISMATCH")
    is_platform = str(user.get("type")) == "-1"
    if is_platform and not {"aiops:agent:edit", "aiops:agent:publish"}.issubset(
        info.get("permissions") or []
    ):
        raise SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    return ManagerIdentity(uid, tenant, is_platform, exchanged["token"])


def tenants(context, caller):
    if not caller.platform:
        raise SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    data = company_get(context, caller.token, "/tenant/list/my")
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
    found = next((t for t in tenants(context, caller) if t["id"] == tenant_id), None)
    if found is None:
        raise SetupRefused("SETUP_TENANT_FORBIDDEN")
    return found
