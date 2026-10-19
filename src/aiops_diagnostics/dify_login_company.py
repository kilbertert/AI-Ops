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

#: 公司网关**验证码过滤器**的拒绝状态码（2026-10-19 实测）。
#:
#: 这是本模块唯一需要声明额外拒绝状态码的地方：那个过滤器在网关里先于密码校验，
#: 三个分支（不能为空 / 失效 / 错误）**都回 428**，语义区分只在响应体的 `msg` 里。
#: 通用默认 `{401, 403}` 对它不成立，于是 428 会被归成「上游不可用」—— 运营输错一次
#: 验证码，看到的是"公司登录服务暂时不可用"。**用户输入问题被说成上游故障。**
COMPANY_CAPTCHA_REJECTED_STATUS = 428


@dataclass(frozen=True, slots=True)
class CompanyIdentity:
    """这次登录交换认定的身份。``token`` 只用于当次读权限码。"""

    user_id: str
    username: str
    tenant_id: str
    token: str = field(repr=False)
    #: 公司 B 端账号种类（``sys_user.type``：-1 平台 / 1 租户 / 2 店主 / 5 代理商…）。
    #: 试运行（#782）要它来判"顶层账号看整租户、其余按店铺→站点"，所以从
    #: ``/upms/user/info`` 的 ``data.sysUser.type`` 读回来。缺失即空串（fail-closed 到空范围）。
    user_type: str = ""


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


def _mapping(*, login: bool) -> ErrorMapping:
    """公司的两类端点各有自己的拒绝语义，所以映射分两份。

    ``login=True``（``/upms/token/login``）：这个端点上的凭据拒绝是**运营的输入问题**
    —— 密码错、账号停用、验证码不对 —— 必须变成 :class:`CompanyLoginRejected`，
    运营才会看到"你输错了"而不是"服务挂了"。

    **只把 428 认作这一类。** 401/403 在登录端点上不是"运营输错了"：实测（2026-10-19）
    密码错是 **200 + `code:-1000`**，而 401/403 在这条链上意味着**网关层面**的问题
    （我们配置的 Basic 凭据、来源限制、网关自身的拒绝）。把它说成"你密码错了"会让运维
    去查一个根本不存在的输错 —— 这与本条要修的缺陷是同一类错误，只是方向相反。

    ``login=False``（``/upms/user/info``）：这里失败不是运营的输入问题，是我们刚拿到
    的那枚令牌不被接受 —— 那是我们自己的链路问题，一律 ``CompanyUnreachable``。
    """

    def rejected(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_AUTH_REJECTED", f"公司侧拒绝了凭据：{failure.detail}")

    def login_rejected(failure: HttpFailure) -> Exception:
        reason = _reason_from_message(failure.detail)
        if reason == _FALLBACK_REJECTION:
            # 走到这里只可能是 428 —— 它只由验证码过滤器产生。报文读不出来也仍然是
            # "验证码这一关没过"，不要说成一句无处可查的中性拒因。
            reason = "验证码不正确或已失效，请重新获取"
        return CompanyLoginRejected("COMPANY_LOGIN_REJECTED", reason)

    def unavailable(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_UNAVAILABLE", f"公司侧不可用：{failure.detail}")

    def malformed(failure: HttpFailure) -> Exception:
        return CompanyUnreachable("COMPANY_MALFORMED", f"公司侧返回无法解释：{failure.detail}")

    return ErrorMapping(
        # 428 = 验证码过滤器（只有登录端点有它）。401/403 有意**不**在这里声明：
        # 它们在本链路上是网关的问题，走 http_error → CompanyUnreachable。
        auth_rejected=login_rejected if login else rejected,
        http_error=unavailable,
        unavailable=unavailable,
        invalid_body=malformed,
        invalid_envelope=malformed,
        auth_rejected_statuses=(
            frozenset({COMPANY_CAPTCHA_REJECTED_STATUS}) if login else frozenset({401, 403})
        ),
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
    # ⚠️ 参数必须走 **query string**，不能放 body（2026-10-16 实测踩到）。
    #
    # 公司的路径是：网关 `PasswordDecoderFilter` 从 `uri.getRawQuery()` 取 `password`
    # 解密后**重写 query** 再转发。参数放 body 的后果是双重的、且都表现为难以归因的错误：
    #   1. `code` 网关看不见 ⇒ 过滤器回「验证码不能为空」（而运营看到的是"服务暂时不可用"）；
    #   2. 就算绕过验证码，`password` 也不会被解密 ⇒ UPMS 拿密文比 bcrypt ⇒ 「密码错误」。
    #
    # 管理后台前端发的是 query（`admin.qumall` 实测抓包：query keys = code, deviceType,
    # grant_type, password, randomStr, scope, username，POST body 为空），所以照它。
    payload = request_json(
        RequestSpec(
            url=join_url(settings.base_url, COMPANY_LOGIN_PATH, query=params),
            method="POST",
            headers=headers,
            timeout=settings.timeout,
        ),
        mapping=_mapping(login=True),
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


#: 判不出具体成因时的中性拒因。**不猜**：猜错会把运营引向错误的方向。
_FALLBACK_REJECTION = "公司登录被拒绝"


def _reason_from_message(message: str) -> str:
    """把公司的中文提示转成"运营看得懂、且不泄漏内部"的一句话。

    三种成因（密码错 / 锁定 / 不存在）在源码里是三个 i18n 键，但**渲染文案来自
    Nacos/i18n 服务**，仓里读不到确切字符串。于是这里按"能不能从 msg 判出来"分流，
    判不出就回一句中性的拒因 —— 猜错文案会把运营引向错误的方向。

    验证码那条**排在密码之前**判：公司的验证码过滤器先于密码校验，所以"验证码错误"
    的报文里不会出现"密码"；反过来若哪天两者同时出现，那是验证码这一关没过，不是密码。
    """
    if "验证码" in message:
        return "验证码不正确或已失效，请重新获取"
    if _MSG_PASSWORD_ERROR in message or "密码" in message:
        return "公司账号或密码不正确"
    if "锁定" in message or "冻结" in message:
        return "公司账号已被锁定或冻结，请联系公司管理员"
    if "不存在" in message:
        return "公司账号不存在"
    return _FALLBACK_REJECTION


def _login_reason(payload: dict[str, Any]) -> str:
    """登录失败（200 + ``code``）时的拒因。见 :func:`_reason_from_message`。"""
    return _reason_from_message(_company_message(payload))


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_user_type(settings: CompanyAuthSettings, identity: CompanyIdentity) -> str:
    """``data.sysUser.type`` —— 顶层/代理商的判据（#782）。

    与 :func:`read_permissions` 打同一个端点，所以调用方**只多调一次**不如一次读全：
    这里单独暴露是因为权限码是"能不能进"，`type` 是"能看多大范围"，两件事分开更清楚。
    读不到即空串：空串**不是**顶层账号（``is_tenant_level_account`` 只认 ``-1/1``），
    因此会走 fail-closed 的店铺→站点解析。
    """
    payload = _user_info(settings, identity)
    sys_user = payload.get("sysUser")
    if not isinstance(sys_user, dict):
        return ""
    value = sys_user.get("type")
    if value is None:
        return ""
    return str(value).strip()[:16]


def _user_info(settings: CompanyAuthSettings, identity: CompanyIdentity) -> dict[str, Any]:
    """GET ``/upms/user/info`` 并返回 **data** 段（权限码与 sysUser 都在里面）。"""
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
        mapping=_mapping(login=False),
        envelope=_raw_envelope,
    )
    if not isinstance(payload, dict):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息返回不是对象")
    if _company_error_code(payload) not in (None, _COMPANY_SUCCESS_CODE):
        raise CompanyUnreachable("COMPANY_INFO_REJECTED", _company_message(payload) or "读取公司用户信息被拒")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息缺少 data")
    return data


def read_permissions(settings: CompanyAuthSettings, identity: CompanyIdentity) -> tuple[str, ...]:
    """用**本次登录拿到的令牌**读权限码；读的是 `data.permissions`。

    `client-type` 必须在这里带上（公司侧从请求线程本地读它，缺省 `admin`）。**未知值
    会得到空角色集** —— 那会表现为"没有权限"，是 fail-closed 的，可以接受；但配置
    写错时运营看到的是"你没权限"，所以部署文档要指明这个值来自公司。
    """
    data = _user_info(settings, identity)
    raw = data.get("permissions")
    if not isinstance(raw, list):
        raise CompanyUnreachable("COMPANY_MALFORMED", "公司用户信息缺少 permissions")
    return tuple(str(item) for item in raw if isinstance(item, str) and item)


def fetch_captcha(settings: CompanyAuthSettings, random_str: str) -> tuple[bytes, str]:
    """取一张公司验证码图，返回 ``(jpeg 字节, content-type)``。

    `randomStr` 由**调用方生成**并一路带到登录：公司那边用它当 Redis 键存答案，
    响应里不带它。所以生成与携带必须是同一个值。

    ## ⚠️ 不要指定 ``Accept``（2026-10-16 实测踩到）

    公司网关对 ``GET /code`` **按 Accept 过滤**：只要 ``Accept`` 不是 ``*/*``（或干脆不发），
    就回 **404** —— 而那个 404 的表现是"验证码取不到"，完全看不出是内容协商。
    实测：``image/jpeg`` / ``image/*`` / ``image/png`` / ``text/html`` 全部 404，
    ``*/*`` 与**不发该头**返回 200 ``image/jpeg``。

    所以这里**故意不带 Accept**（第一版带了 `image/jpeg`，部署后才暴露：
    54 个单测全绿也测不出它 —— 它只在真上游上发生）。
    """
    from aiops_diagnostics.bounded_http import join_url, open_response, read_body

    spec = RequestSpec(
        url=join_url(settings.base_url, COMPANY_CAPTCHA_PATH, query={"randomStr": random_str}),
        method="GET",
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
    "read_user_type",
]
