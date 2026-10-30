"""公司权限码 → AI-Ops 角色（#926 真浏览器验收暴露的缺陷的修法）。

**为什么需要这一层。** ``_require_knowledge_admin`` 判的是
``caller.roles ∩ {ROLE_AGENT_ADMIN, ROLE_PLATFORM_ADMIN}``，而真实身份路径上 ``roles``
**恒为空集**：``third_session_auth`` 与 ``company_token_auth`` 都写死 ``roles=frozenset()``。
于是管理面路由（知识库四条、``/v1/agents``、debug-run）对**任何真实运营**都是 403，
只有构造身份（ADR-0058，直接给了 ``ROLE_AGENT_ADMIN``）能过 —— 而构造身份正是验收用的那种。
单测也照抄了 ``roles={ROLE_AGENT_ADMIN}``，所以全绿。

**公司侧的答案一直存在**：``GET {upms}/user/info`` 的 ``data.permissions``（41 实测 3132 项）
含 ``aiops:agent:edit`` 与 ``aiops:agent:publish``（ADR-0029 建的两个菜单权限码）。
本模块把那两个码翻成角色，让判据回到公司已经在用的东西上。

三个决定，以及为什么：

1. **读不到就是没有权限，不是放行、也不该是 500。** 上游超时/形状不符一律返回空角色集
   （fail closed 的方向），并记一行可区分原因。这与 ``company_token_auth`` 处理店铺归属失败
   是同一条定调：授权依赖不可用，结论只能是「不给」，不是「崩」。
2. **缓存按令牌指纹，不按用户 id。** 权限可以随时被公司撤掉；按用户缓存会把一次撤权
   拖到缓存过期为止。按令牌缓存最多拖到该令牌过期，而那是它本来就有的生命周期。
   缓存只在**进程内**（不落库、不跨进程），凭据不外泄。
3. **``aiops:agent:edit`` ⇒ 全部四个 AGENT 角色。** 公司模型里「能编辑智能体」是一个码，
   而我们内部把它拆成 viewer/admin/publisher 三个。映射成 edit ⇒ admin（含 viewer），
   publish 只由 ``aiops:agent:publish`` 单独给 —— 两个码在公司里本来就分开建（ADR-0029），
   合并会把一个撤权变成一次静默的越权。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from aiops_diagnostics.common.bounded_http import (
    JSON_CONTENT_TYPE,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    bearer_auth_header,
    join_url,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.config import UpmsSettings

_LOGGER = logging.getLogger("aiops.company_permissions")

#: 公司权限码（ADR-0029 在公司 UPMS 里建的那两行 ``sys_menu``）。
EDIT_PERMISSION_CODE = "aiops:agent:edit"
PUBLISH_PERMISSION_CODE = "aiops:agent:publish"

#: 权限码 → 内部角色。**edit 给 admin 而不是 viewer**：能编辑的人当然能看，
#: 而公司没有建「只看」这个码，凭空造一个等于发明一套公司不认的授权。
ROLES_BY_PERMISSION_CODE: dict[str, frozenset[str]] = {
    EDIT_PERMISSION_CODE: frozenset({"ROLE_AGENT_VIEWER", "ROLE_AGENT_ADMIN"}),
    PUBLISH_PERMISSION_CODE: frozenset({"ROLE_AGENT_PUBLISHER"}),
}

#: 公司 ``/user/info`` 的 ``client-type``。与登录代理同一个值，理由也同一条
#: （``dify_login_company.DEFAULT_CLIENT_TYPE``）：角色集按它过滤，换一个值就是另一套角色，
#: 而「谁能编辑」与「谁能进 Dify」必须是同一个身份问题。
DEFAULT_CLIENT_TYPE = "admin"

USER_INFO_PATH = "/user/info"


class CompanyPermissionSource:
    """``PermissionSource`` 的生产实现：读公司权限码、翻成角色、按令牌缓存。

    ``enabled`` 为 False 时**一次上游都不打**并返回空角色集 —— 这是「没有配置公司边界」
    的部署应该得到的答案（与改动前逐字一致），而不是每次请求去撞一个不存在的端点。
    """

    def __init__(
        self,
        settings: UpmsSettings,
        *,
        cache: PermissionCodeCache | None = None,
        client_type: str = DEFAULT_CLIENT_TYPE,
        enabled: bool = True,
        fetcher: Callable[[UpmsSettings, str], tuple[str, ...]] | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache if cache is not None else PermissionCodeCache()
        self._client_type = client_type
        self._enabled = enabled and bool((settings.base_url or "").strip())
        self._fetch = fetcher or (
            lambda s, token: fetch_permission_codes(s, token, client_type=self._client_type)
        )

    def roles_for(self, token: str) -> frozenset[str]:
        if not self._enabled or not token:
            return frozenset()
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        try:
            codes = self._fetch(self._settings, token)
        except Exception as exc:  # noqa: BLE001 - 授权依赖的任何逃逸都只能变成「不给」
            # 注入的 fetcher 也在这里被兜住：调用方换掉实现不该换掉这条结论。
            _LOGGER.info("company permissions unavailable reason=%s", type(exc).__name__)
            return frozenset()
        roles = roles_from_permissions(list(codes))
        self._cache.put(token, roles)
        return roles


def roles_from_permissions(permissions: Any) -> frozenset[str]:
    """把公司权限码集合翻成内部角色。非列表/非字符串项一律忽略（不猜、不报错）。"""
    if not isinstance(permissions, (list, tuple, set, frozenset)):
        return frozenset()
    granted: set[str] = set()
    for code in permissions:
        if isinstance(code, str):
            granted.update(ROLES_BY_PERMISSION_CODE.get(code.strip(), ()))
    return frozenset(granted)


def fetch_permission_codes(
    settings: UpmsSettings,
    token: str,
    *,
    client_type: str = DEFAULT_CLIENT_TYPE,
    timeout: float | None = None,
) -> tuple[str, ...]:
    """用调用者**自己的令牌**读公司权限码。读不到返回空元组（fail closed）。

    ``token`` 是调用者的公司访问令牌 —— 不是服务侧令牌：``/user/info`` 回答的是
    「这个令牌的主人有哪些权限」，用服务身份去问只会得到服务身份自己的答案。
    ``settings.inside_token`` 因此**不**参与这次调用。
    """
    base_url = (settings.base_url or "").strip()
    credential = (token or "").strip()
    if not base_url or not credential:
        return ()

    def _unavailable(failure: HttpFailure) -> Exception:
        # 统一成一个可捕获的形状；调用方不区分「上游挂了」与「形状不对」——
        # 对授权而言两者的结论相同（不给），而原因是给运维看的。
        return _PermissionLookupFailed(failure.kind)

    def _rejected(failure: HttpFailure) -> Exception:
        return _PermissionLookupFailed("rejected")

    try:
        data = request_json(
            RequestSpec(
                url=join_url(base_url, USER_INFO_PATH),
                headers={
                    "Accept": JSON_CONTENT_TYPE,
                    "Authorization": bearer_auth_header(credential),
                    "client-type": client_type,
                },
                timeout=settings.timeout_seconds if timeout is None else timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=_rejected,
                http_error=_unavailable,
                unavailable=_unavailable,
                invalid_body=_unavailable,
                invalid_envelope=_unavailable,
            ),
            envelope=parse_code_data_envelope,
        )
    except _PermissionLookupFailed as exc:
        _LOGGER.info("company permissions unavailable reason=%s", exc.reason)
        return ()
    except Exception as exc:  # noqa: BLE001 - 授权依赖的任何逃逸都只能变成「不给」
        _LOGGER.info("company permissions unavailable reason=%s", type(exc).__name__)
        return ()

    if not isinstance(data, dict):
        _LOGGER.info("company permissions unavailable reason=not_an_object")
        return ()
    raw = data.get("permissions")
    if not isinstance(raw, list):
        _LOGGER.info("company permissions unavailable reason=missing_permissions")
        return ()
    return tuple(code for code in raw if isinstance(code, str) and code)


class _PermissionLookupFailed(RuntimeError):
    """内部哨兵：把 ``bounded_http`` 的失败译成一次可捕获的授权失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PermissionCodeCache:
    """进程内的权限码缓存，键是**令牌指纹**而不是用户 id。

    为什么按令牌：权限随时可能被公司撤掉，按用户缓存会把撤权拖到缓存过期；按令牌最多
    拖到该令牌自己的有效期，而那本来就是它自然的生命周期（同 ``dify_login_service``
    里会话窗口的取舍）。

    指纹用 ``sha256`` 前 16 字节：只需要「同一令牌命中同一条」，不需要抵抗
    已知明文 —— 但也不该把令牌原文当字典键留在内存里。**不打印、不落库。**
    """

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("permission cache ttl must not be negative")
        self._ttl = ttl_seconds
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, frozenset[str]]] = {}

    def get(self, token: str) -> frozenset[str] | None:
        key = _fingerprint(token)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, roles = entry
            if expires_at <= self._now():
                self._entries.pop(key, None)
                return None
            return roles

    def put(self, token: str, roles: frozenset[str]) -> None:
        with self._lock:
            self._entries[_fingerprint(token)] = (self._now() + self._ttl, roles)


def _fingerprint(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
