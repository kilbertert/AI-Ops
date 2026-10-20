"""在 Dify 里为一个公司用户开一个 editor 账号（#772 B / #778）。

三步，都用**官方接口**，且**全程不经过邮件**：

1. 以应急 owner 的**自签会话**调邀请接口 → 得到一个邀请 `token`（Dify 把它放在响应的
   `url` 里，不寄信）。
2. 用同一个 `token` 调 `/console/api/activate` 完成激活。
3. 在 Dify 里按邮箱查回账号 id（`/console/api/workspaces/current/members`）。

**为什么不用自带注册或 `INNER_API`/`ADMIN_API_KEY`**：前者的 `ALLOW_REGISTER` 是 PRD 明确
要保持关闭的；后两者是权力过宽的共享静态密钥（能管整个实例），而这里只需要"邀请一个人"。

**账号邮箱**由公司用户 id 确定性生成、落在不可投递的保留域（``.invalid``）—— 自签会话模型
下没有任何人需要收到那封信，而"同一公司用户永远同一个 Dify 账号"靠的就是这个确定性。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    json_body,
    request_json,
)
from aiops_diagnostics.dify_login_proxy import (
    _MAX_NAME_CHARS,
    ACCOUNT_EMAIL_DOMAIN,
    ConsoleSession,
    DifyLoginError,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.provision")

INVITE_PATH = "/console/api/workspaces/current/members/invite-email"
ACTIVATE_PATH = "/console/api/activate"
MEMBERS_PATH = "/console/api/workspaces/current/members"

#: 公司用户在 Dify 里**只给 editor**（#772 B / 用户故事 9）：拿不到 API 工具密钥、
#: 管不了成员、也不能借工具测试接口让服务端访问任意地址。
MEMBER_ROLE = "editor"


class DifyProvisionError(DifyLoginError):
    """开户/查账号这一步没做成。"""


@dataclass(frozen=True, slots=True)
class DifyConsoleSettings:
    """代理访问 Dify 控制台所需的一切。``secret_key`` 只用于**自签会话**。"""

    base_url: str
    secret_key: str = field(repr=False, default="")
    deployment_edition: str = "COMMUNITY"
    access_expire_minutes: int = 60
    timeout: float = 15.0
    #: 邀请时带上，让激活页语言一致；可选。
    language: str = "zh-Hans"
    #: **本部署的环境标识**（#864）。它参与 Dify 账号邮箱的派生，所以两套 UPMS
    #: 的同名 ``sys_user.id`` 不会撞到同一个 Dify 账号。由部署身份给（见
    #: `AIOPS_LOGIN_PROXY_ENVIRONMENT`），**永不从请求读**。
    environment: str = ""

    def __post_init__(self) -> None:
        # 构造时就用**与派生时完全相同**的那条判据校验（#864）。只查"非空"是不够的：
        # 那样 `prod.41` 能让代理正常启动，然后在**每一个已授权用户**登录时抛
        # DifyProvisionError —— 表现成"登录服务坏了"，而不是"配置写错了"。
        # 一个配置错误必须在启动时报，不能在请求路径上报。
        _canonical_environment(self.environment)


def account_email_for(company_user_id: str, *, environment: str) -> str:
    """(环境, 公司用户 id) → **确定性** Dify 账号邮箱（用户故事 14；#864 加环境）。

    ## 为什么必须带环境（#864）

    Dify 账号是**全局**的（不属于任何 workspace），身份由邮箱唯一确定。而 41 与 95 是
    两套独立 UPMS、两套独立数据库，``sys_user.id`` 各自生成。**撞号是可能的**，而撞号的
    后果不是"登录失败"，是**越权**：第二个环境的代理会为同一个 Dify 账号签会话，
    于是 B 环境的那个人能进 A 环境那个人的 workspace。

    环境前缀把两个 id 空间隔开。**这是唯一能隔开它的地方** —— Dify 侧没有 workspace
    维度的账号身份，账号就是账号。

    ## 确定性

    同样的 ``(environment, company_user_id)`` 永远得到同一个邮箱 —— 这是硬要求：
    同一用户二次登录必须落到同一个账号，否则历史编辑与发布记录会断。
    所以不做任何随机化或哈希。

    ``environment`` 是**部署身份**（每台机器一个值），不是请求参数：它由调用方从自己的
    配置里取，永不从浏览器来的任何字段推导。

    ## 环境标识**不归一化**，只校验（#864 评审）

    第一版对环境做了与 id 相同的"把不安全字符换成 ``-``"。那会造出一个新的撞号方式：
    ``prod.41`` 与 ``prod/41`` 归一化后都成了 ``prod-41`` —— 两个部署可以配置出两个
    不同的字符串，却仍然落到同一个 Dify 账号，正好把这条缺陷放回来。

    所以环境走**白名单校验**而不是归一化：必须已经是规范形式（小写字母/数字/``-``/``_``，
    以字母或数字开头结尾）。不合规就**报错**，让部署者改配置，而不是替他猜。
    """
    prefix = _canonical_environment(environment)
    safe = _safe_fragment(company_user_id, "COMPANY_USER_ID_INVALID", "公司用户 id 不含可用字符")
    return f"{prefix}-{safe}@{ACCOUNT_EMAIL_DOMAIN}"


#: 环境标识的规范形式。**归一化不在选项里** —— 见 `account_email_for` 的 docstring。
_ENVIRONMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")


def _canonical_environment(value: str) -> str:
    """环境标识必须**已经是**规范形式；不做任何字符替换。"""
    normalized = (value or "").strip()
    if not _ENVIRONMENT_RE.match(normalized):
        raise DifyProvisionError(
            "ENVIRONMENT_INVALID",
            "环境标识必须是规范形式（小写字母/数字/`-`/`_`，以字母或数字开头结尾，≤32）",
        )
    return normalized


def _safe_text(value: str) -> str:
    """收窄成邮箱局部部分可用的字符；空了返回空串（调用方决定怎么处理）。"""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (value or "")).strip("-")


def _safe_fragment(value: str, code: str, message: str) -> str:
    """收窄成邮箱局部部分可用的字符；空了就是配置错，不是"随便凑一个"。"""
    safe = _safe_text(value)
    if not safe:
        raise DifyProvisionError(code, message)
    return safe


def account_name_for(company_user_id: str, username: str) -> str:
    """展示名：用户名优先，退回归一化后的 id；Dify 上限 30 字符。"""
    candidate = (username or company_user_id).strip() or company_user_id
    return candidate[:_MAX_NAME_CHARS]


def _mapping() -> ErrorMapping:
    def rejected(failure: HttpFailure) -> Exception:
        return DifyProvisionError("DIFY_AUTH_REJECTED", f"Dify 拒绝了会话：{failure.detail}")

    def unavailable(failure: HttpFailure) -> Exception:
        return DifyProvisionError("DIFY_UNAVAILABLE", f"Dify 不可用：{failure.detail}")

    def malformed(failure: HttpFailure) -> Exception:
        return DifyProvisionError("DIFY_MALFORMED", f"Dify 返回无法解释：{failure.detail}")

    return ErrorMapping(
        auth_rejected=rejected,
        http_error=unavailable,
        unavailable=unavailable,
        invalid_body=malformed,
        invalid_envelope=malformed,
    )


def _map_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    raise mapping.invalid_envelope(HttpFailure(kind="invalid_envelope", detail="invalid response"))


def _console_headers(session: ConsoleSession) -> dict[str, str]:
    """控制台的**双提交**：Bearer + `X-CSRF-Token` + 与头逐字相同的 csrf cookie。

    cookie 名必须带 ``session.cookie_prefix``：Dify 的 ``check_csrf_token`` 要求头与
    **它自己读到的那个 cookie** 逐字相同，而 https 下 `_real_cookie_name` 会给名字加
    前缀 ⇒ 写死裸名会读到空值，表现为 `CSRF token is missing or invalid` 的 401。
    开户这一跳（`invite-email`）走 `@login_required`，撞上它的后果是**新账号开不出来**，
    而**已有映射的账号完全不受影响** —— 间歇性、极难定位。
    """
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {session.access_token}",
        "X-CSRF-Token": session.csrf_token,
        "Cookie": f"{session.cookie_prefix}csrf_token={session.csrf_token}",
    }


def _post(settings: DifyConsoleSettings, session: ConsoleSession, path: str, payload: dict[str, Any]) -> Any:
    headers = _console_headers(session)
    headers["Content-Type"] = "application/json"
    return request_json(
        RequestSpec(
            url=settings.base_url.rstrip("/") + path,
            method="POST",
            headers=headers,
            body=json_body(payload),
            timeout=settings.timeout,
        ),
        mapping=_mapping(),
        envelope=_map_envelope,
    )


def _activate_without_session(settings: DifyConsoleSettings, payload: dict[str, Any]) -> Any:
    """用**邀请 token 本身**激活，**不带任何会话**（ADR-0023 的开户第二步）。

    ## 为什么必须不带会话（2026-10-16 实测）

    Dify 的 `ActivateApi.post` 里有一句：

    ```python
    if extract_access_token(request) is not None:
        authenticated_account_id = current_account_with_tenant().account.id
    ```

    而我们的开户是**以 owner 的身份**去激活**别人**的邀请 —— 于是
    `account_activation.activate(..., authenticated_account_id=<owner>)` 与邀请里编码的账号
    不符，抛 `InvitationAccountMismatchError`。**而 `_post` 的返回值被丢弃、异常被吞**，
    所以这一步**静默失败**：映射照建、会话照签、登录照能用，只有 `accounts.status`
    留在 `pending`。这就是它难被发现的原因。

    token-only 是 Dify 明确保留的路径（docstring 原文："Token-only activation remains
    available for legacy clients."），也正是这一步需要的语义：激活凭的是邀请 token。
    """
    return request_json(
        RequestSpec(
            url=settings.base_url.rstrip("/") + ACTIVATE_PATH,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body=json_body(payload),
            timeout=settings.timeout,
        ),
        mapping=_mapping(),
        envelope=_map_envelope,
    )


def _get(settings: DifyConsoleSettings, session: ConsoleSession, path: str) -> Any:
    return request_json(
        RequestSpec(
            url=settings.base_url.rstrip("/") + path,
            method="GET",
            headers=_console_headers(session),
            timeout=settings.timeout,
        ),
        mapping=_mapping(),
        envelope=_map_envelope,
    )


def _invite_token(result: Any, email: str) -> str | None:
    """从邀请响应里取出该邮箱的激活 token，或（已存在则）返回 ``None``。

    Dify 把 token 放在 `url` 的查询串里（`.../activate?email=…&token=…`），这就是
    "不寄信也能激活"的全部机制 —— 它是**接口语义**里的字段，不是从页面里抠的。

    ## ⚠️ 键名是 ``invitation_results``，不是 ``results``（2026-10-16 实测）

    本函数原来只读 ``results``，而 Dify 1.17.1 实际回的顶层键是
    **``invitation_results``**，且 ``results`` 经常**根本不存在**。后果是一条
    **完全静默**的失败链：解析不到 token ⇒ ``provision_editor`` 里
    ``if token is not None`` 为假 ⇒ **激活那一步被整个跳过** ⇒ 映射照建、会话照签、
    登录照用，只有 ``accounts.status`` 永远留在 ``pending``。

    为什么它这么难发现：**没有任何一处报错**，而且 pending 的账号在这个自签会话模型下
    **照样能登录**（会话是验签 + 查库，不查 status）。所以既要修解析，也要修"跳过"
    这个设计 —— 见 :func:`provision_editor` 里对"拿不到 token"的显式处理。

    两个键都接受：Dify 还有其他版本/路径回 ``results``，少读一个就少一条通路。
    """
    if not isinstance(result, dict):
        return None
    items: list[Any] = []
    for key in ("invitation_results", "results"):
        value = result.get(key)
        if isinstance(value, list):
            items.extend(value)
    for item in items:
        if not isinstance(item, dict) or str(item.get("email", "")).lower() != email.lower():
            continue
        status = str(item.get("status", ""))
        if status == "already_member":
            return None
        url = item.get("url")
        if not isinstance(url, str) or "token=" not in url:
            continue
        query = urllib_parse_qs(url)
        token = query.get("token")
        if token and token[0]:
            return token[0]
    return None


def urllib_parse_qs(url: str) -> dict[str, list[str]]:
    import urllib.parse

    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def find_account_id(settings: DifyConsoleSettings, session: ConsoleSession, email: str) -> str:
    """在成员列表里按邮箱查回 Dify 账号 id（**不建重复账号**的前提）。"""
    payload = _get(settings, session, MEMBERS_PATH)
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list):
        raise DifyProvisionError("DIFY_MALFORMED", "成员列表响应缺少 accounts")
    for account in accounts:
        if isinstance(account, dict) and str(account.get("email", "")).lower() == email.lower():
            account_id = account.get("id")
            if isinstance(account_id, str) and account_id:
                return account_id
    raise DifyProvisionError("DIFY_ACCOUNT_MISSING", "邀请/激活后仍找不到该账号")


def provision_editor(
    settings: DifyConsoleSettings,
    *,
    owner_session: ConsoleSession,
    company_user_id: str,
    username: str,
) -> str:
    """确保该邮箱在 Dify 里是一个 editor 账号，返回它的账号 id。

    幂等：账号已存在（`already_member`）时跳过邀请，直接查回 id。这样"二次登录"与
    "首次登录"走同一条代码路径，不会给同一个人开出第二个账号。
    """
    email = account_email_for(company_user_id, environment=settings.environment)
    invited = _post(
        settings,
        owner_session,
        INVITE_PATH,
        {"emails": [email], "role": MEMBER_ROLE, "language": settings.language},
    )
    token = _invite_token(invited, email)
    if token is not None:
        activated = _activate_without_session(
            settings,
            {
                "email": email,
                "token": token,
                "name": account_name_for(company_user_id, username),
                "interface_language": settings.language,
                "timezone": "Asia/Shanghai",
            },
        )
        # 这一步以前是 `_post(...)` 且**丢弃返回值**，异常也不看 ⇒ 失败是静默的
        # （账号留在 pending，而登录照样能用）。现在显式检查：激活不了就明确报错，
        # 因为"开户没完成"是应当被看见的故障，不是可以忽略的细节。
        _require_activated(activated, email)
    else:
        # 邀请成功（新账号）却拿不到 token ⇒ 激活会被整个跳过，账号停在 pending。
        # **不能静默**：这正是那个"pending 却能登录"的缺陷的成因（2026-10-16）。
        # 唯一合法的"没有 token"是 `already_member`（账号早就激活过了）—— 那种情况下
        # 下面这一次 `find_account_id` 会查回一个真正的成员，本就是幂等路径。
        if not _is_already_member(invited, email):
            raise DifyProvisionError("DIFY_INVITE_TOKEN_MISSING", f"邀请成功但取不到激活 token（{email}）")
    return find_account_id(settings, owner_session, email)


def _is_already_member(result: Any, email: str) -> bool:
    """邀请响应里这个邮箱是不是 ``already_member``（⇒ 不需要激活，是幂等路径）。"""
    if not isinstance(result, dict):
        return False
    for key in ("invitation_results", "results"):
        for item in result.get(key) or []:
            if (
                isinstance(item, dict)
                and str(item.get("email", "")).lower() == email.lower()
                and str(item.get("status", "")) == "already_member"
            ):
                return True
    return False


def _require_activated(result: Any, email: str) -> None:
    """激活必须真的成功。判据是 Dify 的回执，不是"没抛异常"。

    实测 Dify 1.17.1 的 `/activate` 成功时回的是
    ``{"data": {"result": "success"}, "code": 0, ...}``（信封）或扁平
    ``{"result": "success"}``，两种都接受；其余形状一律当失败
    —— 静默接受一个没激活的账号，正是这条分支以前的老毛病。
    """
    candidates: list[Any] = [result]
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        candidates.append(result["data"])
    for candidate in candidates:
        if isinstance(candidate, dict) and str(candidate.get("result", "")).lower() == "success":
            return
    raise DifyProvisionError("DIFY_ACTIVATE_FAILED", f"激活未完成（{email}）")


__all__ = [
    "MEMBER_ROLE",
    "DifyConsoleSettings",
    "DifyProvisionError",
    "account_email_for",
    "account_name_for",
    "find_account_id",
    "provision_editor",
]
