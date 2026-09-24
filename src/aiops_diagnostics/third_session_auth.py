from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import redis

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
)
from aiops_diagnostics.config import Settings, UpmsSettings
from aiops_diagnostics.query_scope import (
    QueryScope,
    UpmsShopDirectory,
    resolve_operator_site_scope,
)
from aiops_diagnostics.scope_context import (
    C_MAPPING_AMBIGUOUS,
    C_MAPPING_FAILED,
    C_MAPPING_NOT_CONFIGURED,
    C_MAPPING_NOT_FOUND,
    C_MAPPING_TENANT_MISMATCH,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    ScopeError,
    SubjectRecord,
    UpmsDirectory,
)
from aiops_diagnostics.sources import SourceError, mysql_site_mapper

_LOGGER = logging.getLogger(__name__)

#: 会话解析出的 C 端身份在拿不到 B 端主体时使用的占位 ``b_user_id`` 前缀。
#: 占位值只在 C 端用户没有 B 端账号（消费者端的正常状态）时出现，必须配合
#: ``SubjectRecord.b_subject_reason`` 解读，绝不能当作 B 端 ``SysUser.id`` 使用。
C_SUBJECT_PLACEHOLDER_PREFIX = "c:"


@dataclass(frozen=True, slots=True)
class ThirdSessionSettings:
    host: str
    port: int
    database: int
    username: str = ""
    password: str = field(repr=False, default="")
    service_token: str = field(repr=False, default="")
    key_prefix: str = "app:3rd_session:"
    timeout_seconds: int = 5


class BSubjectDirectory(Protocol):
    """既有 C→B 身份映射接缝：C 端 ``SysUser.userId`` → B 端主体集合。

    实现复用 UPMS ``GET /user/inside/byUserId/{userId}``，不新增服务端接口。
    """

    def users_by_c_user_id(self, c_user_id: str) -> tuple[SubjectRecord, ...]: ...


class UpmsBSubjectDirectory:
    """``BSubjectDirectory`` 的 UPMS 实现（复用既有 C→B 映射端点）。

    ``credential`` 是服务侧配置的内部调用凭据，只透传给 UPMS，绝不进入
    ``ScopeContext``、审计摘要或错误消息。缺配置时不构造本类——管家端路径随后
    按 fail closed 拒绝，消费者端路径不受影响。
    """

    def __init__(self, settings: UpmsSettings, credential: str) -> None:
        self._directory = UpmsDirectory(settings)
        self._credential = credential

    def users_by_c_user_id(self, c_user_id: str) -> tuple[SubjectRecord, ...]:
        return self._directory.users_by_c_user_id(self._credential, c_user_id)


class OperatorSiteScope(Protocol):
    """运营商站点范围接缝（#426）：B 端主体 + 租户 → 冻结的 ``QueryScope``。

    实现必须复用后端权威授权所用的同一数据源（``/shopuser/getShops``）与既有
    站点归属映射（``ch_site.shop_id → ch_site.id``）；未绑定店铺、已绑定店铺在
    充电库里没有站点都返回**空集合**（下游据此拒绝），上游不可达或响应形状非法
    以 ``ScopeError`` 失败关闭。
    """

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> QueryScope: ...


class UpmsOperatorSiteScope:
    """``OperatorSiteScope`` 的生产实现（#426，四跳链的后两跳）。

    店铺集合取 ``GET /shopuser/getShops``——后端权威授权（``@ShopDataScope`` 的
    隔离集合来源）所用的同一个端点；站点集合走既有归属映射。映射查询与受限直连
    共用同一条跳板隧道（``mysql_site_mapper``），因此 SSH 部署下不会为授权另开
    一条绕过隧道的连接路径。

    ``credential`` 是服务侧配置的内部调用凭据，只透传给 UPMS，绝不进入
    ``ScopeContext``、审计摘要或错误消息。
    """

    def __init__(self, settings: Settings, credential: str) -> None:
        self.settings = settings
        self._shops = UpmsShopDirectory(settings.upms, credential)

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> QueryScope:
        with mysql_site_mapper(self.settings) as mapper:
            return resolve_operator_site_scope(b_user_id, tenant_id, shops=self._shops, mapper=mapper)


class RedisThirdSessionResolver:
    """C 端 ``thirdSession`` → ``ScopeContext`` 的 Redis 解析路径。

    身份由两部分组成：会话给出的 C 端 ``userId``（消费者端可见性判定的依据，恒等于
    会话值）与经既有 C→B 映射端点补全的 B 端 ``sys_user.id``（管家端授权链的起点）。
    ``b_subject_directory`` 未注入、或解析不出**同租户内唯一**的 B 端主体时，身份只保留
    C 侧部分并带上可区分原因——消费者端行为不变，需要 B 端主体的下游自行 fail closed。

    业务数据范围由主体决定（#426）：拿不到唯一 B 端主体时仍是解析器的固定兜底值
    ``self``（消费者端逐字不变）；拿到时替换为该主体名下的**运营商站点集合**——
    替换而非取交集，因为 ``self`` 只是兜底值而不是管家端的业务规则，取交集等于只
    保留「自己下的单」，正是本 PRD 要修掉的错位。同一会话的订单授权判定与证据收集
    因此共用这一个范围（``resolve_query_scope``）。
    """

    def __init__(
        self,
        settings: ThirdSessionSettings,
        *,
        b_subject_directory: BSubjectDirectory | None = None,
        operator_scope: OperatorSiteScope | None = None,
    ) -> None:
        if not settings.password or not settings.service_token:
            raise ValueError("thirdSession Redis and service credentials are required")
        self.settings = settings
        self.b_subject_directory = b_subject_directory
        self.operator_scope = operator_scope

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        if not secrets.compare_digest(token, self.settings.service_token):
            raise CallerAuthError("service authentication failed", code=CALLER_AUTH_INVALID)
        if not third_session or len(third_session) > 256 or any(c in third_session for c in "\r\n"):
            raise CallerAuthError("thirdSession is invalid", code=CALLER_AUTH_INVALID)
        try:
            client = redis.Redis(
                host=self.settings.host,
                port=self.settings.port,
                db=self.settings.database,
                username=self.settings.username or None,
                password=self.settings.password,
                decode_responses=False,
                socket_connect_timeout=self.settings.timeout_seconds,
                socket_timeout=self.settings.timeout_seconds,
            )
            raw = client.get(f"{self.settings.key_prefix}{third_session}")
            if raw and b"{" not in raw:
                pointer = _decode_java_string(raw)
                if pointer.startswith(self.settings.key_prefix):
                    pointer = pointer[len(self.settings.key_prefix) :]
                if pointer and pointer.startswith("wx:"):
                    raw = client.get(f"{self.settings.key_prefix}{pointer}")
        except redis.RedisError as exc:
            raise CallerAuthError(
                "thirdSession store unavailable", code=CALLER_AUTH_UNAVAILABLE, retryable=True
            ) from exc
        if not raw:
            raise CallerAuthError("thirdSession invalid or expired", code=CALLER_AUTH_INVALID)
        try:
            payload: Any = _decode_session_payload(raw)
        except json.JSONDecodeError as exc:
            raise CallerAuthError("thirdSession payload invalid", code=CALLER_AUTH_INVALID) from exc
        if not isinstance(payload, dict):
            raise CallerAuthError("thirdSession payload invalid", code=CALLER_AUTH_INVALID)
        user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
        tenant_id = str(payload.get("tenantId") or payload.get("tenant_id") or "").strip()
        if not user_id or not tenant_id:
            raise CallerAuthError("thirdSession subject invalid", code=CALLER_AUTH_INVALID)
        subject = self._resolve_subject(user_id, tenant_id)
        caller = SubjectRecord(b_user_id="service:java-bff", tenant_id=tenant_id)
        return ScopeContext.build(
            caller=caller,
            subject=subject,
            # 会话查的是自己的授权范围，不是代查他人的目标主体。保持 True 会进入
            # 代查分支：未配 Dis 则报错，配了则与 Dis 点位取交集而收窄（PRD #423
            # 已定调置 False，且该标志应由请求意图推导，而不是按调用者类型写死）。
            delegated=False,
            effective_tenant_id=tenant_id,
            data_scope=self._data_scope(subject, tenant_id),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )

    def _data_scope(self, subject: SubjectRecord, tenant_id: str) -> DataScope:
        """会话的业务数据范围：有唯一 B 端主体时取其运营商站点集合。

        解析失败一律失败关闭为**空集合**——拒绝全部订单查询，不回落为租户级放行，
        也绝不套用 ``self``（那会把「查不到自己的运营商范围」伪装成「只能看本人的
        单」）。失败覆盖链路上每一种逃逸方式：上游不可达/响应形状非法/数量超界
        （``ScopeError``）、充电库不可用（``SourceError``）、范围 ID 不可用
        （``ValueError``）；不捕获就等于让一次授权故障变成 500。

        失败原因单独记一行，与 #425 区分的两种空集合成因（账号漏登记 /
        已登记店铺无站点）都不是一回事：那些要去补数据，这里要去看上游。
        """
        directory = self.operator_scope
        if subject.b_subject_reason or directory is None:
            return DataScope(type=SCOPE_TYPE_SELF)
        try:
            scope = directory.site_scope_by_b_user_id(subject.b_user_id, tenant_id)
        except (ScopeError, SourceError, ValueError) as exc:
            _log_operator_scope_unavailable(exc)
            return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
        return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=scope.site_ids)

    def _resolve_subject(self, user_id: str, tenant_id: str) -> SubjectRecord:
        """会话主体：C 端 id 恒等于会话 ``userId``，B 端 id 由既有 C→B 映射补全。

        C 端用户没有 B 端账号是消费者端的正常状态，因此映射结论只记录原因、不拒绝
        请求，消费者端可见性判定零变化；需要唯一 B 端主体的下游（管家端订单授权）
        必须对非空 ``SubjectRecord.b_subject_reason`` fail closed，而不是把占位
        ``b_user_id`` 当作 B 端主体使用。
        """
        directory = self.b_subject_directory
        if directory is None:
            return _c_side_subject(user_id, tenant_id, C_MAPPING_NOT_CONFIGURED)
        try:
            records = directory.users_by_c_user_id(user_id)
        except (ScopeError, ValueError) as exc:
            _log_unresolved(C_MAPPING_FAILED, getattr(exc, "code", type(exc).__name__))
            return _c_side_subject(user_id, tenant_id, C_MAPPING_FAILED)
        if not records:
            # 消费者端的正常状态：不记日志，避免每个 C 端账号都刷一行。
            return _c_side_subject(user_id, tenant_id, C_MAPPING_NOT_FOUND)
        if len(records) > 1:
            _log_unresolved(C_MAPPING_AMBIGUOUS)
            return _c_side_subject(user_id, tenant_id, C_MAPPING_AMBIGUOUS)
        record = records[0]
        if record.tenant_id != tenant_id:
            # 跨租户的 C→B 映射不是本次会话的身份，宁可没有 B 端主体也不越租户。
            _log_unresolved(C_MAPPING_TENANT_MISMATCH)
            return _c_side_subject(user_id, tenant_id, C_MAPPING_TENANT_MISMATCH)
        # C 端 id 与租户以会话为准：端点回带的同名字段不参与可见性判定，
        # 避免响应形状差异改写 self 范围的查询谓词。
        return replace(record, c_user_id=user_id, tenant_id=tenant_id, b_subject_reason="")


def _log_unresolved(reason: str, code: str | None = None) -> None:
    """把必须由运维处置的 C→B 映射结论写进日志：不含用户 id、租户与凭据。"""
    if code is None:
        _LOGGER.info("third_session c_mapping_unresolved reason=%s", reason)
    else:
        _LOGGER.info("third_session c_mapping_unresolved reason=%s code=%s", reason, code)


def _log_operator_scope_unavailable(error: Exception) -> None:
    """运营商站点范围解析失败：可区分原因（错误码或异常类型），不含身份标识。

    与 #425 的 ``no_shop_binding`` / ``shop_without_site`` 都不同：那两种是数据/
    配置缺口（拒绝是正确行为，运营去补登记），这里是解析本身失败（运维去看上游，
    不是去补绑定）。三种混在一起就无法从日志判断「权限正确拒绝」与「系统坏了」。
    """
    _LOGGER.info(
        "third_session operator_scope_unavailable code=%s",
        getattr(error, "code", None) or type(error).__name__,
    )


def _c_side_subject(user_id: str, tenant_id: str, reason: str) -> SubjectRecord:
    """拿不到唯一 B 端主体时的 C 侧身份：占位 ``b_user_id`` + 可区分原因。"""
    return SubjectRecord(
        b_user_id=f"{C_SUBJECT_PLACEHOLDER_PREFIX}{user_id}",
        c_user_id=user_id,
        tenant_id=tenant_id,
        b_subject_reason=reason,
    )


def _decode_session_payload(raw: str | bytes) -> dict[str, Any]:
    """Read the JSON string embedded by Java ObjectOutputStream without deserializing objects."""
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    start = data.find(b"{")
    end = data.rfind(b"}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("session JSON not found", data.decode("utf-8", "ignore"), 0)
    payload = json.loads(data[start : end + 1].decode("utf-8"))
    if not isinstance(payload, dict):
        raise json.JSONDecodeError("session JSON is not an object", "", 0)
    return payload


def _decode_java_string(raw: bytes) -> str:
    start = raw.find(b"t\x00")
    if start < 0:
        return ""
    size = raw[start + 2]
    return raw[start + 3 : start + 3 + size].decode("utf-8", "ignore")
