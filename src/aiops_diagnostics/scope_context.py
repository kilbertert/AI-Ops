"""权限上下文与身份映射（单次诊断运行专用）。

本模块把一次诊断运行的授权输入收敛为一个不可变的 ``ScopeContext``：

- 调用者身份只能来自有效平台凭证，绝不由请求体里的 ``user_id`` 推导；
- 目标主体（被排查对象）与调用者分离，只有调用者具备平台既有的代查/管理能力时才允许使用；
- B 端 ``SysUser.id``、绑定的 C 端 ``SysUser.userId``、``tenant_id``、``organ_id``、
  ``shop_id/site_id`` 与业务数据范围是显式区分的字段，不会互相顶替；
- 主体不存在、映射歧义、租户越权、范围为空或 UPMS/Dis 不可用时立即 fail closed，
  不回退为无范围直查，也不会触发任何业务数据库查询（解析器不持有任何数据源）。

平台依赖通过 ``PlatformDirectory`` 单一接缝接入：UPMS 提供用户完整信息、业务数据范围
（``/user/ds``）和 C 端到 B 端映射（``/user/inside/byUserId/{userId}``）；
Dis 点位到站点的归属解析在受限查询任务（T2/T3）中以同一接缝加入，其失败同样 fail closed。

凭证只透传给目录服务，绝不进入 ``ScopeContext``、审计摘要或错误消息；
``ScopeContext`` 也不持久化任何数据库凭据或完整权限副本，审计面只暴露摘要和范围指纹。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aiops_diagnostics.bounded_http import (
    JSON_CONTENT_TYPE,
    EnvelopeParser,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    bearer_auth_header,
    join_url,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.config import UpmsSettings

SCOPE_ERROR_AUTH_FAILED = "scope.auth_failed"
SCOPE_ERROR_CONFIG_MISSING = "scope.config_missing"
SCOPE_ERROR_UPMS_UNAVAILABLE = "scope.upms_unavailable"
SCOPE_ERROR_SUBJECT_NOT_FOUND = "scope.subject_not_found"
SCOPE_ERROR_AMBIGUOUS_SUBJECT = "scope.ambiguous_subject"
SCOPE_ERROR_DELEGATION_DENIED = "scope.delegation_denied"
SCOPE_ERROR_TENANT_FORBIDDEN = "scope.tenant_forbidden"
SCOPE_ERROR_EMPTY_SCOPE = "scope.empty_scope"

#: C 端 → B 端映射未能唯一确定 B 端主体时的可区分原因（``SubjectRecord.b_subject_reason``）。
#: 空串表示已唯一确定；其余取值表示**没有**可用的 B 端主体，需要 B 端主体的下游必须
#: 据此拒绝，而不是把 ``b_user_id`` 当作 B 端 ``SysUser.id`` 使用。
C_MAPPING_NOT_CONFIGURED = "mapping_not_configured"
C_MAPPING_FAILED = "mapping_failed"
C_MAPPING_NOT_FOUND = "subject_not_found"
C_MAPPING_AMBIGUOUS = "ambiguous_subject"
C_MAPPING_TENANT_MISMATCH = "tenant_mismatch"

SCOPE_TYPE_ALL = "all"
SCOPE_TYPE_ORGAN = "organ"
SCOPE_TYPE_SELF = "self"

_SCOPE_TYPES = frozenset({SCOPE_TYPE_ALL, SCOPE_TYPE_ORGAN, SCOPE_TYPE_SELF})

USER_INFO_PATH = "/user/info"
DATA_SCOPE_PATH = "/user/ds"
USER_BY_B_ID_PATH = "/user/inside/byId"
USER_BY_C_USER_ID_PATH = "/user/inside/byUserId"
ROLE_LIST_PATH = "/role/list"
SHOP_USER_PATH = "/shopuser/getShops"

_SCOPE_TYPE_BY_PLATFORM_CODE = {
    0: SCOPE_TYPE_ALL,
    1: SCOPE_TYPE_ORGAN,
    2: SCOPE_TYPE_ORGAN,
    3: SCOPE_TYPE_ORGAN,
    4: SCOPE_TYPE_SELF,
}

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _raw_payload(payload: Any, *, mapping: ErrorMapping) -> Any:
    """无信封端点（``/role/list`` 返回裸 JSON 数组）：解码后的 JSON 原样返回。

    形状判断留给调用方，与迁移前一致：骨架自带的 ``parse_raw_envelope`` 会把标量
    载荷判为信封不符，而本端点的旧实现把任何形状都交给调用方，因此这里保留原样
    返回，``invalid_envelope`` 永不被触发。
    """
    return payload


class ScopeError(RuntimeError):
    """A scope resolution failed and the diagnostic run must fail closed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _unavailable_message(failure: HttpFailure) -> str:
    """UPMS 请求失败文案：信封层与 HTTP/传输层措辞不同。

    骨架对信封层拒绝（``code`` 不为 0/200）与 HTTP/传输层故障都归入
    ``unavailable``，但旧实现给两者不同的文案，因此按 ``failure.cause``
    分流：骨架只在信封层留空 ``cause``。
    """
    if failure.cause is None:
        return f"UPMS 拒绝查询: {failure.detail}"
    detail = f"HTTP {failure.status}" if failure.status is not None else failure.detail
    return f"UPMS 请求失败: {detail}"


def _rejected_message(failure: HttpFailure) -> str:
    """凭证被拒文案：HTTP 层与信封层措辞不同，同样按 ``failure.cause`` 分流。"""
    if failure.cause is None:
        return f"UPMS 拒绝查询: {failure.detail}"
    return "UPMS 拒绝平台凭证"


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """One platform role granted to a user, plus the roles it inherits."""

    code: str
    parent_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubjectRecord:
    """A resolved platform user identity.

    ``b_user_id`` is the B 端 ``SysUser.id``; ``c_user_id`` is the bound C 端
    ``SysUser.userId``. They are never interchangeable.

    A third-session identity completes ``b_user_id`` through the existing C→B
    mapping endpoint (``third_session_auth``). When that mapping cannot name
    exactly one B 端 subject inside the session tenant, ``b_subject_reason``
    carries a distinguishable cause and ``b_user_id`` remains a C-side
    placeholder that must never be used as a B 端 subject.
    """

    b_user_id: str
    c_user_id: str | None = None
    username: str = ""
    tenant_id: str | None = None
    organ_id: str | None = None
    shop_id: str | None = None
    b_subject_reason: str = ""


@dataclass(frozen=True, slots=True)
class DataScope:
    """业务数据范围：范围类型与明确区分的组织/店铺/站点集合。

    ``all`` 表示平台全量可见（通常是平台管理员），``organ`` 表示由 ID 集合限定，
    ``self`` 表示仅本人记录。``organ`` 类型没有任何 ID 时视为范围为空。
    """

    type: str = SCOPE_TYPE_ORGAN
    organ_ids: tuple[str, ...] = ()
    shop_ids: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.type not in _SCOPE_TYPES:
            raise ValueError(f"未知业务数据范围类型: {self.type}")


@dataclass(frozen=True, slots=True)
class UserContext:
    """The full permission context of the caller resolved from the platform."""

    subject: SubjectRecord
    roles: tuple[RoleGrant, ...] = ()
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    """Authorization inputs for one diagnostic run.

    ``credential`` is the opaque platform credential forwarded to the directory;
    it never becomes part of the resolved context.
    """

    credential: str
    target_b_user_id: str | None = None
    target_c_user_id: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """平台既有的代查/管理权限码与管理角色码。

    ponytail: 默认空集合意味着部署在提供平台真实权限码之前，任何调用者都不能代查
    目标主体或切换租户（fail closed）。升级触发条件：T5 接入运行时时把平台真实
    权限码/角色码写入配置并传入本策略。
    """

    delegated_lookup_permissions: frozenset[str] = frozenset()
    tenant_admin_roles: frozenset[str] = frozenset()


class PlatformDirectory(Protocol):
    """The platform identity/permission services used to resolve a scope."""

    def user_info(self, credential: str) -> UserContext: ...

    def user_by_b_user_id(self, credential: str, b_user_id: str) -> SubjectRecord | None: ...

    def users_by_c_user_id(self, credential: str, c_user_id: str) -> tuple[SubjectRecord, ...]: ...

    def data_scope(self, credential: str) -> DataScope: ...


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """一次诊断运行专用的不可变权限上下文。

    调用者与目标主体是两个独立身份；有效租户、业务数据范围、调用者有效角色（含
    继承）和能力权限共同确定本次运行允许的查询范围。``scope_fingerprint`` 是上述
    范围语义的确定性哈希，供审计归因使用。
    """

    caller: SubjectRecord
    subject: SubjectRecord
    delegated: bool
    effective_tenant_id: str
    data_scope: DataScope
    roles: frozenset[str]
    permissions: frozenset[str]
    resolved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    scope_fingerprint: str = ""

    def __post_init__(self) -> None:
        expected = _scope_fingerprint(
            caller=self.caller,
            subject=self.subject,
            delegated=self.delegated,
            effective_tenant_id=self.effective_tenant_id,
            data_scope=self.data_scope,
            roles=self.roles,
        )
        if not self.scope_fingerprint:
            object.__setattr__(self, "scope_fingerprint", expected)
        elif self.scope_fingerprint != expected:
            raise ValueError("scope_fingerprint 与上下文范围不一致")

    @classmethod
    def build(
        cls,
        *,
        caller: SubjectRecord,
        subject: SubjectRecord,
        delegated: bool,
        effective_tenant_id: str,
        data_scope: DataScope,
        roles: frozenset[str],
        permissions: frozenset[str],
        resolved_at: datetime | None = None,
    ) -> ScopeContext:
        return cls(
            caller=caller,
            subject=subject,
            delegated=delegated,
            effective_tenant_id=effective_tenant_id,
            data_scope=data_scope,
            roles=frozenset(roles),
            permissions=frozenset(permissions),
            resolved_at=resolved_at or datetime.now(UTC),
            scope_fingerprint="",
        )

    def audit_summary(self) -> dict[str, Any]:
        """Auditable scope summary without credentials or permission copies."""
        return {
            "caller_b_user_id": self.caller.b_user_id,
            "caller_username": self.caller.username,
            "subject_b_user_id": self.subject.b_user_id,
            "subject_c_user_id": self.subject.c_user_id,
            "delegated": self.delegated,
            "effective_tenant_id": self.effective_tenant_id,
            "data_scope_type": self.data_scope.type,
            "scope_fingerprint": self.scope_fingerprint,
            "resolved_at": self.resolved_at.isoformat(),
        }


class ScopeResolver:
    """Resolve one ``ScopeContext`` from a credential plus optional target conditions."""

    def __init__(self, directory: PlatformDirectory, *, policy: ScopePolicy | None = None) -> None:
        self.directory = directory
        self.policy = policy or ScopePolicy()

    def resolve(self, request: ScopeRequest) -> ScopeContext:
        credential = (request.credential or "").strip()
        target_b_user_id = _clean_optional(request.target_b_user_id)
        target_c_user_id = _clean_optional(request.target_c_user_id)
        tenant_id = _clean_optional(request.tenant_id)
        if target_b_user_id and target_c_user_id:
            raise ScopeError(
                "同时提供 B 端用户 ID 和 C 端用户 ID，目标主体存在歧义",
                code=SCOPE_ERROR_AMBIGUOUS_SUBJECT,
            )
        if not credential:
            raise ScopeError("平台凭证为空", code=SCOPE_ERROR_AUTH_FAILED)

        caller = self.directory.user_info(credential)
        caller_roles = _expand_roles(caller.roles)
        subject, delegated = self._resolve_subject(
            credential, target_b_user_id, target_c_user_id, caller, caller_roles
        )
        effective_tenant_id = self._effective_tenant(tenant_id, caller, subject, caller_roles)
        data_scope = self.directory.data_scope(credential)
        _require_scope(data_scope)
        return ScopeContext.build(
            caller=caller.subject,
            subject=subject,
            delegated=delegated,
            effective_tenant_id=effective_tenant_id,
            data_scope=data_scope,
            roles=caller_roles,
            permissions=caller.permissions,
        )

    def _resolve_subject(
        self,
        credential: str,
        target_b_user_id: str | None,
        target_c_user_id: str | None,
        caller: UserContext,
        caller_roles: frozenset[str],
    ) -> tuple[SubjectRecord, bool]:
        if target_b_user_id is None and target_c_user_id is None:
            return caller.subject, False
        if not self._may_delegate(caller.permissions, caller_roles):
            raise ScopeError(
                "调用者不具备代查/管理权限，不能使用目标主体",
                code=SCOPE_ERROR_DELEGATION_DENIED,
            )
        if target_b_user_id is not None:
            subject = self.directory.user_by_b_user_id(credential, target_b_user_id)
            if subject is None:
                raise ScopeError(
                    f"目标 B 端用户不存在: {target_b_user_id}",
                    code=SCOPE_ERROR_SUBJECT_NOT_FOUND,
                )
            return subject, True
        records = self.directory.users_by_c_user_id(credential, target_c_user_id)
        if not records:
            raise ScopeError(
                f"目标 C 端用户不存在或未绑定 B 端用户: {target_c_user_id}",
                code=SCOPE_ERROR_SUBJECT_NOT_FOUND,
            )
        if len(records) > 1:
            raise ScopeError(
                f"目标 C 端用户映射到多个 B 端用户，存在歧义: {target_c_user_id}",
                code=SCOPE_ERROR_AMBIGUOUS_SUBJECT,
            )
        return records[0], True

    def _may_delegate(self, permissions: frozenset[str], roles: frozenset[str]) -> bool:
        return bool(
            permissions.intersection(self.policy.delegated_lookup_permissions)
            or roles.intersection(self.policy.tenant_admin_roles)
        )

    def _effective_tenant(
        self,
        requested_tenant_id: str | None,
        caller: UserContext,
        subject: SubjectRecord,
        caller_roles: frozenset[str],
    ) -> str:
        caller_tenant_id = _clean_optional(caller.subject.tenant_id)
        subject_tenant_id = _clean_optional(subject.tenant_id)
        candidate = requested_tenant_id if requested_tenant_id is not None else subject_tenant_id
        if candidate is None:
            raise ScopeError("有效租户无法确定", code=SCOPE_ERROR_EMPTY_SCOPE)
        if caller_roles.intersection(self.policy.tenant_admin_roles):
            return candidate
        if caller_tenant_id is None or candidate != caller_tenant_id:
            raise ScopeError(
                "普通调用者不能通过目标主体或租户参数扩大租户范围",
                code=SCOPE_ERROR_TENANT_FORBIDDEN,
            )
        return candidate


class UpmsDirectory:
    """``PlatformDirectory`` 的 UPMS HTTP 实现。

    复用平台现有用户能力，调用方凭证以 ``Authorization: Bearer`` 透传，绝不落盘
    或写入错误消息：

    - ``GET /user/info``：调用者完整信息，含 ``sysUser``（``id`` 为 B 端
      ``SysUser.id``，``userId`` 为绑定的 C 端用户）、角色（含继承关系）和菜单权限；
    - ``GET /user/ds``：业务数据范围（范围类型 + 组织/店铺/站点 ID）；
    - ``GET /user/inside/byId/{id}``：按 B 端用户 ID 返回基础用户对象，仅用于
      目标主体身份，不当作完整权限上下文；
    - ``GET /user/inside/byUserId/{userId}``：C 端用户到 B 端用户的映射；
    - ``GET /shopuser/getShops``：B 端用户到店铺的归属（后端权威授权同一端点）。

    ponytail: 端点路径与响应字段按 PRD #23 记录的 cloud-upms 能力固定，当前只由
    离线契约测试守护；真实环境验收时若字段不同，只需调整本类的解析，解析器与
    ``ScopeContext`` 不受影响。响应形状无法识别时一律 fail closed。
    """

    def __init__(self, settings: UpmsSettings) -> None:
        self.settings = settings

    def user_info(self, credential: str) -> UserContext:
        data = self._get(USER_INFO_PATH, credential)
        if not isinstance(data, dict):
            raise ScopeError("UPMS 用户信息响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        return UserContext(
            subject=_parse_subject(data.get("sysUser")),
            roles=_parse_roles(data.get("roles")),
            permissions=_parse_string_set(data.get("permissions"), field="permissions"),
        )

    def user_by_b_user_id(self, credential: str, b_user_id: str) -> SubjectRecord | None:
        data = self._get(f"{USER_BY_B_ID_PATH}/{_safe_path_segment(b_user_id)}", credential)
        return None if data is None else _parse_subject(data)

    def users_by_c_user_id(self, credential: str, c_user_id: str) -> tuple[SubjectRecord, ...]:
        data = self._get(f"{USER_BY_C_USER_ID_PATH}/{_safe_path_segment(c_user_id)}", credential)
        if data is None:
            return ()
        records = data if isinstance(data, list) else [data]
        return tuple(_parse_subject(item) for item in records)

    def shop_ids_by_b_user_id(self, credential: str, b_user_id: str) -> tuple[str, ...]:
        """B 端 ``SysUser.id`` → 店铺 ID 集合（只读）。

        复用后端权威授权所用的同一个端点：``ShopIdInterceptor``（``@ShopDataScope``
        的隔离集合来源）取的正是 ``GET /shopuser/getShops?userId=``。因此调用方
        不是在另发明一套范围。未绑定任何店铺时返回**空集合**——空集合在下游表示
        失败关闭，不得被改写成「不限制店铺」。

        响应形状沿用 2026-09-01 已对生产 UPMS 实测通过的解析（ID 数组或逗号
        分隔字符串）；形状不可识别时以 ``SCOPE_ERROR_UPMS_UNAVAILABLE`` 失败关闭。
        """
        shops = self._get(f"{SHOP_USER_PATH}?userId={_safe_path_segment(b_user_id)}", credential)
        return _parse_id_list(shops)

    def data_scope(self, credential: str) -> DataScope:
        try:
            data = self._get(DATA_SCOPE_PATH, credential)
        except ScopeError as exc:
            # 平台 ``/user/ds`` 存在缺陷（组织 ID 列表为空时生成非法
            # ``IN ()`` SQL，对所有用户 500）。凭证问题仍然直接失败关闭；
            # 服务侧错误降级为「角色数据权限 + 店铺归属」推导，不扩大范围。
            if exc.code == SCOPE_ERROR_AUTH_FAILED:
                raise
            return self._derive_data_scope_from_roles(credential)
        if not isinstance(data, dict):
            raise ScopeError("UPMS 数据范围响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        return DataScope(
            type=_normalize_scope_type(data.get("type")),
            organ_ids=_parse_id_tuple(data.get("organIds")),
            shop_ids=_parse_id_tuple(data.get("shopIds")),
            site_ids=_parse_id_tuple(data.get("siteIds")),
        )

    def _derive_data_scope_from_roles(self, credential: str) -> DataScope:
        """``/user/ds`` 不可用时的降级推导。

        仍然使用平台既有数据范围机制：角色 ``dsType``（``DataScopeTypeEnum``：
        0=全部、1=自定义、2=本级及子级、3=本级）决定范围类型；``dsScope``
        （自定义范围 ID，逗号分隔）提供组织集合；``/shopuser/getShops``
        提供店铺集合。取调用者角色中最宽的一个 ``dsType``；调用者角色不可
        识别或无法推导时以 ``SCOPE_ERROR_UPMS_UNAVAILABLE`` 失败关闭。
        """
        info = self._get(USER_INFO_PATH, credential)
        if not isinstance(info, dict):
            raise ScopeError("UPMS 用户信息响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        sys_user = info.get("sysUser") or {}
        caller_role_ids = {str(item) for item in (sys_user.get("roleIds") or []) if _optional_text(item)}
        caller_b_user_id = _optional_text(sys_user.get("id"))
        caller_organ_id = _optional_text(sys_user.get("organId"))
        if not caller_b_user_id:
            raise ScopeError("UPMS 用户对象缺少 B 端用户 ID", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        if not caller_role_ids:
            raise ScopeError("UPMS 用户角色不可识别，无法推导数据范围", code=SCOPE_ERROR_UPMS_UNAVAILABLE)

        roles_payload = self._request(ROLE_LIST_PATH, credential)
        if not isinstance(roles_payload, list):
            raise ScopeError("UPMS 角色列表响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        ds_type: int | None = None
        custom_ids: tuple[str, ...] = ()
        for item in roles_payload:
            if not isinstance(item, dict):
                raise ScopeError("UPMS 角色列表响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
            if str(item.get("id") or "") not in caller_role_ids:
                continue
            item_type = _optional_text(item.get("dsType"))
            if item_type is None:
                continue
            value = _to_int(item_type)
            if value is None or value not in _SCOPE_TYPE_BY_PLATFORM_CODE:
                raise ScopeError(
                    f"UPMS 角色数据权限类型无法识别: {item_type}",
                    code=SCOPE_ERROR_UPMS_UNAVAILABLE,
                )
            if ds_type is None or value < ds_type:
                ds_type = value
                custom_ids = _parse_id_list(item.get("dsScope"))
        if ds_type is None:
            raise ScopeError("UPMS 角色未配置数据权限类型", code=SCOPE_ERROR_UPMS_UNAVAILABLE)

        shop_ids = self.shop_ids_by_b_user_id(credential, caller_b_user_id)

        scope_type = _SCOPE_TYPE_BY_PLATFORM_CODE[ds_type]
        organ_ids: tuple[str, ...] = ()
        if scope_type == SCOPE_TYPE_ORGAN:
            organ_ids = custom_ids if ds_type == 1 else ((caller_organ_id,) if caller_organ_id else ())
        return DataScope(type=scope_type, organ_ids=organ_ids, shop_ids=shop_ids, site_ids=())

    def _request(self, path: str, credential: str, *, envelope: EnvelopeParser = _raw_payload) -> Any:
        """Fetch one UPMS path and return the payload after *envelope* is applied.

        The transport skeleton owns request assembly, the failure capture set and
        the ``(401, 403)`` status table. This client declares its own domain
        errors and which envelope the endpoint speaks: ``/role/list`` answers
        with a bare JSON array (``_raw_payload``), every other endpoint with a
        ``code``/``data`` envelope. Both layers share one ``ErrorMapping``, so its
        callables discriminate on ``failure.cause`` — the skeleton leaves it
        ``None`` exactly for an envelope-level rejection, whose messages differ
        from the HTTP/transport ones.
        """
        base_url = self.settings.base_url
        if not base_url:
            raise ScopeError("UPMS 服务地址未配置", code=SCOPE_ERROR_CONFIG_MISSING)
        token = (credential or "").strip()
        if not token:
            raise ScopeError("平台凭证为空", code=SCOPE_ERROR_AUTH_FAILED)

        def _rejected(failure: HttpFailure) -> Exception:
            return ScopeError(_rejected_message(failure), code=SCOPE_ERROR_AUTH_FAILED)

        def _unavailable(failure: HttpFailure) -> Exception:
            return ScopeError(_unavailable_message(failure), code=SCOPE_ERROR_UPMS_UNAVAILABLE)

        return request_json(
            RequestSpec(
                url=join_url(base_url, path),
                headers={
                    "Authorization": bearer_auth_header(token),
                    "Accept": JSON_CONTENT_TYPE,
                },
                timeout=self.settings.timeout_seconds,
            ),
            mapping=ErrorMapping(
                auth_rejected=_rejected,
                http_error=_unavailable,
                unavailable=_unavailable,
                invalid_body=_unavailable,
                invalid_envelope=_unavailable,
            ),
            envelope=envelope,
        )

    def _get(self, path: str, credential: str) -> Any:
        """Fetch one UPMS path and return the ``data`` of its ``code``/``data`` envelope."""
        return self._request(path, credential, envelope=parse_code_data_envelope)


def _expand_roles(roles: tuple[RoleGrant, ...]) -> frozenset[str]:
    """Flatten granted roles with their inherited parents into one effective set."""
    parents_by_code = {grant.code: grant.parent_codes for grant in roles}
    effective: set[str] = set()
    for grant in roles:
        pending = [grant.code]
        visited: set[str] = set()
        while pending:
            code = pending.pop()
            if code in visited:
                continue
            visited.add(code)
            effective.add(code)
            pending.extend(parents_by_code.get(code, ()))
    return frozenset(effective)


def _require_scope(data_scope: DataScope) -> None:
    if data_scope.type == SCOPE_TYPE_ORGAN and not (
        data_scope.organ_ids or data_scope.shop_ids or data_scope.site_ids
    ):
        raise ScopeError(
            "业务数据范围为空，无法限定诊断查询范围",
            code=SCOPE_ERROR_EMPTY_SCOPE,
        )


def _scope_fingerprint(
    *,
    caller: SubjectRecord,
    subject: SubjectRecord,
    delegated: bool,
    effective_tenant_id: str,
    data_scope: DataScope,
    roles: frozenset[str],
) -> str:
    # B 端主体 id 已由 ``subject_b_user_id`` 覆盖（#424 的 C→B 映射因此自动进指纹）。
    # ``b_subject_reason`` 有意不入指纹：它不改变任何可见性，只说明 B 端主体为何缺失。
    payload = {
        "caller_b_user_id": caller.b_user_id,
        "subject_b_user_id": subject.b_user_id,
        "subject_c_user_id": subject.c_user_id,
        "delegated": delegated,
        "effective_tenant_id": effective_tenant_id,
        "data_scope": {
            "type": data_scope.type,
            "organ_ids": sorted(data_scope.organ_ids),
            "shop_ids": sorted(data_scope.shop_ids),
            "site_ids": sorted(data_scope.site_ids),
        },
        "roles": sorted(roles),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _safe_path_segment(value: str) -> str:
    """Whitelist user identifiers before they are interpolated into URL paths."""
    text = (value or "").strip()
    if not _SAFE_PATH_SEGMENT.fullmatch(text) or not text.strip("."):
        raise ValueError("用户 ID 包含不允许的字符")
    return text


def _optional_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    return text or None


def _parse_subject(value: Any) -> SubjectRecord:
    if not isinstance(value, dict):
        raise ScopeError("UPMS 用户对象响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    b_user_id = _optional_text(value.get("id"))
    if not b_user_id:
        raise ScopeError("UPMS 用户对象缺少 B 端用户 ID", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    return SubjectRecord(
        b_user_id=b_user_id,
        c_user_id=_optional_text(value.get("userId")),
        username=_optional_text(value.get("username")) or "",
        tenant_id=_optional_text(value.get("tenantId")),
        organ_id=_optional_text(value.get("organId")),
        shop_id=_optional_text(value.get("shopId")),
    )


def _parse_roles(value: Any) -> tuple[RoleGrant, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ScopeError("UPMS 角色响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    grants: list[RoleGrant] = []
    for item in value:
        if isinstance(item, str):
            code = item.strip()
            if code:
                grants.append(RoleGrant(code=code))
            continue
        if not isinstance(item, dict):
            raise ScopeError("UPMS 角色响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        code = _optional_text(item.get("roleCode"))
        if not code:
            raise ScopeError("UPMS 角色对象缺少 roleCode", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        grants.append(RoleGrant(code=code, parent_codes=_parse_id_tuple(item.get("parentCodes"))))
    return tuple(grants)


def _parse_string_set(value: Any, *, field: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ScopeError(f"UPMS {field} 响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    result: set[str] = set()
    for item in value:
        text = _optional_text(item)
        if text is None:
            raise ScopeError(f"UPMS {field} 响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        result.add(text)
    return frozenset(result)


def _parse_id_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ScopeError("UPMS 范围 ID 响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    result: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text is None:
            raise ScopeError("UPMS 范围 ID 响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
        if text not in result:
            result.append(text)
    return tuple(result)


def _parse_id_list(value: Any) -> tuple[str, ...]:
    """Parse IDs from a JSON array or a comma-separated string (role dsScope)."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",") if item.strip()]
        return tuple(dict.fromkeys(parts))
    if isinstance(value, list):
        return _parse_id_tuple(value)
    raise ScopeError("UPMS 范围 ID 响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)


def _normalize_scope_type(value: Any) -> str:
    if value is None:
        return SCOPE_TYPE_ORGAN
    if isinstance(value, bool):
        raise ScopeError("UPMS 数据范围类型响应无效", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    if isinstance(value, int):
        mapped = _SCOPE_TYPE_BY_PLATFORM_CODE.get(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        mapped = text if text in _SCOPE_TYPES else _SCOPE_TYPE_BY_PLATFORM_CODE.get(_to_int(text))
    else:
        mapped = None
    if mapped is None:
        raise ScopeError(f"UPMS 数据范围类型无法识别: {value!r}", code=SCOPE_ERROR_UPMS_UNAVAILABLE)
    return mapped


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
