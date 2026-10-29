"""Dify/Java 试用共用的身份、范围与结果合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.identity.scope_context import ScopeContext, SubjectRecord


class TrialRefused(RuntimeError):
    """试运行的输入不可用 —— 拒绝，而不是猜一个默认值跑下去。"""


@dataclass(frozen=True, slots=True)
class TrialInitiator:
    """发起试运行的**公司用户**（#782），由桥从登录代理的映射表转达。

    四个字段都来自**桥**（服务端到服务端），浏览器提交的同名字段一律被忽略 ——
    试运行的范围只能由身份决定，不能由请求决定。缺任一字段 ⇒ 桥没给身份 ⇒ 退回
    #781 的固定调试租户行为（仍是 fail-closed：只见那一个租户）。
    """

    b_user_id: str
    tenant_id: str
    user_type: str
    username: str = ""

    #: 公司体系的**平台哨兵值**。`sys_user.tenant_id` / `type` 都可能是它，
    #: 而它不是一个租户 —— 两个独立事实共用同一个字面量。
    PLATFORM_TENANT_SENTINEL = "-1"

    @classmethod
    def from_headers(
        cls,
        b_user_id: str | None,
        tenant_id: str | None,
        user_type: str | None,
        username: str | None = None,
    ) -> TrialInitiator | None:
        b, t = (b_user_id or "").strip(), (tenant_id or "").strip()
        if not b or not t:
            return None
        if t == cls.PLATFORM_TENANT_SENTINEL:
            # 平台账号（`type=-1`）的 `tenant_id` 也是 `-1`，但 `-1` **不是租户**：
            # 充电库里没有任何一行 `tenant_id='-1'`。把它当租户下推，得到的不是
            # "看整个平台"，而是一条**范围为空**的查询 —— 每张订单都 404。
            #
            # 实测（2026-10-20，41）：同一请求体、同一把钥匙、同一个已绑定 app，
            # 只把 `X-AIOps-Initiator-Tenant` 从 `-1` 换成 `1942…` 就从
            # `404 ORDER_NOT_FOUND` 变成 `202`。
            #
            # 判成"没有可用身份"而不是"平台看全部"：本路由的可见范围由身份给出，
            # 而"平台"在公司体系里的诚实含义是**跨租户**，这个服务没有任何一个
            # 跨租户可用的数据源去支撑它。回落成调试租户是既有的、被验收过的行为
            # （ADR-0028 §3），而不是新的越权。
            return None
        return cls(
            b_user_id=b[:128],
            tenant_id=t[:128],
            user_type=(user_type or "").strip()[:16],
            username=(username or "").strip()[:128],
        )


def build_trial_scope(initiator: TrialInitiator, *, operator_scope: Any) -> ScopeContext:
    """把发起人的公司身份变成试运行的可见范围（#782），规则与**会话路径同一份**。

    镜像 ``third_session_auth`` 的 ``_data_scope``：顶层账号（``type ∈ {-1,1}``）看整个
    租户；其余（代理商）走 ``OperatorSiteScope`` 的「店铺 → 站点」解析；解析失败一律
    fail-closed 成**空站点集合**（拒绝全部订单），不回落成租户级放行、也不套用 ``self``
    —— 后者会把"查不到范围"伪装成"只能看本人的单"。

    ``operator_scope`` 就是网关**已有**的那条链（``UpmsOperatorSiteScope``：UPMS
    ``/shopuser/getShops`` + 既有隧道上的站点归属映射），不新开连接路径。
    """
    from aiops_diagnostics.identity.scope_context import (
        SCOPE_TYPE_ALL,
        SCOPE_TYPE_ORGAN,
        SCOPE_TYPE_SELF,
        DataScope,
        is_tenant_level_account,
    )

    if is_tenant_level_account(initiator.user_type):
        data_scope = DataScope(type=SCOPE_TYPE_ALL)
    elif operator_scope is None:
        # 没配 UPMS/凭据 ⇒ 解析不出运营商范围。**不放宽**：落到"什么都看不到"。
        data_scope = DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
    else:
        try:
            scope = operator_scope.site_scope_by_b_user_id(initiator.b_user_id, initiator.tenant_id)
            data_scope = DataScope(type=SCOPE_TYPE_ORGAN, site_ids=scope.site_ids)
        except Exception:  # noqa: BLE001 - 解析失败一律 fail-closed（同会话路径）
            data_scope = DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
    del SCOPE_TYPE_SELF  # 只用作文档：本路径**不**用 self 兜底
    subject = SubjectRecord(
        b_user_id=initiator.b_user_id,
        tenant_id=initiator.tenant_id,
        user_type=initiator.user_type,
    )
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=initiator.tenant_id,
        data_scope=data_scope,
        roles=frozenset(),
        permissions=frozenset(),
    )


@dataclass(frozen=True, slots=True)
class TrialResult:
    """一次试运行的产出：诊断报告 + 证据 + 被挡的工具 + 数据源失败。"""

    status: str
    report: dict[str, Any]
    evidence_ids: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    failed_sources: tuple[str, ...]
    language: str
