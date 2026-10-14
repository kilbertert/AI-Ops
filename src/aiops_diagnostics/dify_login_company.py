"""公司侧的登录与授权读取（#772 B / #778）—— 登录代理用来做**身份结论**的那一半。

三个调用，顺序固定，且每一步的失败都明确分成两类（"凭据被拒"与"服务不可用"），
因为代理要对运营说清楚下一步该做什么。

## 公司 API 的四个形状事实（源码核实，`cloud-upms` `release` / `cloud-gateway` `release`）

1. **登录体是查询串/表单参数，不是 JSON**：`@RequestParam username, password, ...`。
2. **密码要 AES 加密**：网关的 `PasswordDecoderFilter` 在转发前解密 `password` 参数。
   明文会被解成乱码，表现为登录失败。编码见 :func:`dify_login_proxy.encrypt_company_password`。
3. **登录失败是 HTTP 200 + `code:-1000`**，只有中文 `msg` 区分原因（密码错 / 账号停用 /
   账号不存在）。所以"看状态码"会把失败读成成功。
4. **权限码在 `data.permissions`**，而 `data.sysUser` 是用户对象；角色由**这次请求自己**
   带的 `client-type` 头决定（缺省 `admin`；**未知值得到空角色集**）。

## 验证码由公司网关管

`GET /code?randomStr=<我们生成的 key>` 返回 JPEG；登录时把 `randomStr` 与用户填的
`code` 一起发。`randomStr` 是**我们**生成的（不是从响应里读的），所以要一路带到登录。

**严禁两个豁免**：`X-checkCode: ignore` 头与 `client-type: tenant-app`。它们都在网关里
是"跳过验证码"的开关（`ValidateCodeGatewayFilter`），用了就等于把登录强度降到比公司后台更弱。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    form_body,
    request_json,
)
from aiops_diagnostics.dify_login_proxy import (
    _COMPANY_SUCCESS_CODE,
    COMPANY_CAPTCHA_PATH,
    COMPANY_LOGIN_PATH,
    COMPANY_USER_INFO_PATH,
    CompanyLoginRejected,
    CompanyUnreachable,
    decode_company_token,
    encrypt_company_password,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.company")

#: 登录的 `client-type`。它决定 `/user/info` 取哪些角色 —— 因此代理读权限码时必须
#: 用**同一个**值，否则"能登录"与"能看到权限码"会用两套角色集，而这是同一个身份问题。
DEFAULT_CLIENT_TYPE = "admin"

#: 公司登录失败的中文提示会原样带回给运营（它就是给运营看的），但**不透出内部码**。
_MSG_PASSWORD_ERROR = "用户名或密码错误"
_MSG_LOCKED = "账号已被锁定"
_MSG_NOT_EXISTS = "账号不存在"


@dataclass(frozen=True, slots=True)
class CompanyIdentity:
    """这次登录交换认定的身份。``token`` 只用于当次读权限码。"""

    user_id: str
    username: str
    tenant_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CompanyAuthSettings:
    base_url: str
    client_type: str = DEFAULT_CLIENT_TYPE
    #: 读权限码用的 `client-type`；与登录分开配，因为它们在公司侧是两件事。
    info_client_type: str = ""
    encode_key: str = field(repr=False, default="")
    timeout: float = 10.0
    #: 登录请求要带的 Basic 头（公司网关要求 `Authorization: Basic …`）。留空即不带。
    basic_auth: str = field(repr=False, default="")


def _mapping() -> ErrorMapping:
    def rejected(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_AUTH_REJECTED", f"公司侧拒绝了凭据：{failure.detail}")

    def unavailable(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_UNAVAILABLE", f"公司侧不可用：{failure.detail}")

    def malformed(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_MALFORMED", f"公司侧返回无法解释：{failure.detail}")

    return ErrorMapping(
        auth_rejected=rejected,
        http_error=unavailable,
        unavailable=unavailable,
        invalid_body=malformed,
        invalid_envelope=malformed,
    )


def _raw_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    """无信封：登录成功返回的就是一个**扁平 map**（不是 `{code,data}`）。"""
    if isinstance(payload, dict):
        return payload
    raise mapping.invalid_envelope(HttpFailure(kind="invalid_envelope", detail="invalid response"))


def _company_error_code(payload: dict[str, Any]) -> int | None:
    code = payload.get("code")
    return code if isinstance(code, int) else None


def _company_message(payload: dict[str, Any]) -> str:
    msg = payload.get("msg")
    return msg.strip() if isinstance(msg, str) else ""


def company_login(
    settings: CompanyAuthSettings,
    *,
    username: str,
    password: str,
    random_str: str,
    code: str,
) -> CompanyIdentity:
    """完成一次公司登录交换，返回**我们自己**拿到的身份。

    这里拿到的令牌是这次登录的直接产物，所以它上面的 `user_id` / `username` 是可信的
    —— 不是因为签名可信（它不可信），而是因为**我们亲手完成了这次交换**。
    """
    from aiops_diagnostics.bounded_http import join_url

    if not settings.encode_key:
        raise CompanyUnreachable("COMPANY_KEY_MISSING", "未配置公司密码 AES key（security.encode.key）")
    encrypted = encrypt_company_password(password, settings.encode_key)
    params = {
        "username": username,
        "password": encrypted,
        "randomStr": random_str,
        "code": code,
        "grant_type": "password",
        "scope": "server",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", "client-type": settings.client_type}
    if settings.basic_auth:
        headers["Authorization"] = settings.basic_auth
    payload = request_json(
        RequestSpec(
            url=join_url(settings.base_url, COMPANY_LOGIN_PATH),
            method="POST",
            headers=headers,
            body=form_body(params),
            timeout=settings.timeout,
        ),
        mapping=_mapping(),
        envelope=_raw_envelope,
    )
    if not isinstance(payload, dict):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司登录返回不是对象")
    # 失败也是 200：判 `code` 而不是状态码。
    code_value = _company_error_code(payload)
    token = payload.get("access_token")
    if code_value not in (None, _COMPANY_SUCCESS_CODE) or not isinstance(token, str) or not token:
        raise CompanyLoginRejected("COMPANY_LOGIN_REJECTED", _login_reason(payload))
    claims = decode_company_token(token)
    user_id = _first_str(claims.get("id"), payload.get("id"), claims.get("user_id"))
    username_value = _first_str(payload.get("username"), claims.get("username")) or username
    if not user_id:
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司登录返回里没有用户 id")
    return CompanyIdentity(
        user_id=user_id,
        username=username_value,
        tenant_id=_first_str(payload.get("tenant_id"), claims.get("tenant_id")) or "",
        token=token,
    )


def _login_reason(payload: dict[str, Any]) -> str:
    """把公司的中文提示转成"运营看得懂、且不泄漏内部"的一句话。

    三种成因（密码错 / 锁定 / 不存在）在源码里是三个 i18n 键，但**渲染文案来自
    Nacos/i18n 服务**，仓里读不到确切字符串。于是这里按"能不能从 msg 判出来"分流，
    判不出就回一句中性的拒因 —— 猜错文案会把运营引向错误的方向。
    """
    msg = _company_message(payload)
    if _MSG_PASSWORD_ERROR in msg or "密码" in msg:
        return "公司账号或密码不正确"
    if "锁定" in msg or "冻结" in msg:
        return "公司账号已被锁定或冻结，请联系公司管理员"
    if "不存在" in msg:
        return "公司账号不存在"
    if "验证码" in msg:
        return "验证码不正确或已失效，请重新获取"
    return "公司登录被拒绝"


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_permissions(settings: CompanyAuthSettings, identity: CompanyIdentity) -> tuple[str, ...]:
    """用**本次登录拿到的令牌**读权限码；读的是 `data.permissions`。

    `client-type` 必须在这里带上（公司侧从请求线程本地读它，缺省 `admin`）。**未知值
    会得到空角色集** —— 那会表现为"没有权限"，是 fail-closed 的，可以接受；但配置
    写错时运营看到的是"你没权限"，所以部署文档要指明这个值来自公司。
    """
    from aiops_diagnostics.bounded_http import join_url

    client_type = settings.info_client_type or settings.client_type
    payload = request_json(
        RequestSpec(
            url=join_url(settings.base_url, COMPANY_USER_INFO_PATH),
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {identity.token}",
                "client-type": client_type,
            },
            timeout=settings.timeout,
        ),
        mapping=_mapping(),
        envelope=_raw_envelope,
    )
    if not isinstance(payload, dict):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息返回不是对象")
    if _company_error_code(payload) not in (None, _COMPANY_SUCCESS_CODE):
        raise CompanyUnreachable("COMPANY_INFO_REJECTED", _company_message(payload) or "读取公司用户信息被拒")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息缺少 data")
    raw = data.get("permissions")
    if not isinstance(raw, list):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息缺少 permissions")
    return tuple(str(item) for item in raw if isinstance(item, str) and item)


def fetch_captcha(settings: CompanyAuthSettings, random_str: str) -> tuple[bytes, str]:
    """取一张公司验证码图，返回 ``(jpeg 字节, content-type)``。

    `randomStr` 由**调用方生成**并一路带到登录：公司那边用它当 Redis 键存答案，
    响应里不带它。所以生成与携带必须是同一个值。
    """

    from aiops_diagnostics.bounded_http import join_url, open_response, read_body

    spec = RequestSpec(
        url=join_url(settings.base_url, COMPANY_CAPTCHA_PATH, query={"randomStr": random_str}),
        method="GET",
        headers={"Accept": "image/jpeg"},
        timeout=settings.timeout,
    )
    try:
        response = open_response(spec)
    except Exception as exc:  # noqa: BLE001 - 统一成不可用
        raise CompanyUnreachable("COMPANY_CAPTCHA_UNAVAILABLE", "取公司验证码失败") from exc
    with response:
        body = read_body(response, max_read_bytes=256 * 1024)
        content_type = response.headers.get("Content-Type") or "image/jpeg"
    if not body:
        raise CompanyUnreachable("COMPANY_CAPTCHA_EMPTY", "公司验证码为空")
    return body, content_type


__all__ = [
    "DEFAULT_CLIENT_TYPE",
    "CompanyAuthSettings",
    "CompanyIdentity",
    "company_login",
    "fetch_captcha",
    "read_permissions",
]
