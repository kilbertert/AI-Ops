"""试运行（#772 F / #781）：拿**一份草稿**在真实 harness 上跑一次。

## 它为什么不是第二个执行器

PRD #772 把试运行的形状写死成一句话：**同一条执行路径的另一个入口**。所以这个模块
不构造任何自己的执行链 —— 它调用 ``run_agent_diagnosis``，也就是 ``aiops diagnose``
CLI 走的**同一个**函数，只是把两样东西换成了草稿里的值：

* ``methodology`` —— 草稿的方法论提示词；
* ``allowed_tools`` —— 草稿的工具白名单。

其余全部照旧：``DiagnosticToolExecutor``（白名单在执行器里 fail-closed 强制）、证据日志、
只读数据源、语言守卫、``AgentResultValidator``、结构契约、以及**预算**（``max_turns`` 与
``max_tool_calls`` 来自同一份 ``Settings``，所以"试运行与生产同一预算"是构造出来的，
不是承诺出来的 —— 没有第二份可以漂走的数字）。

草稿**只能收窄**工具集合，不能新增枚举外的工具：白名单里的名字要过
``ToolName`` 校验，七个之外直接拒。这一条与 ``allowed_tools`` 在发布门上的检查同一形状。

## 它为什么**不是**网关的对话诊断路径

网关的 ``start_standard_diagnosis`` 带着会话轮次、turn 注册、interrupt RPC、conversation
store —— 那些是**对话面**需要的，试运行一个都不要（它是一次性的，不落会话）。硬把草稿
塞进那条路径反而会退化成"复制一份执行链"，正是 PRD 要避免的。所以这里直接走 CLI 那个
入口，短路掉对话机制。

## 范围与副作用

* 订单范围**不来自请求**：调用方（网关路由）传进来的是一个**已由配置决定**的
  ``ScopeContext``（固定调试租户的 ``admin_context``）。请求体里的任何租户/站点/用户
  字段都不进这个函数 —— 它只接 ``order_no`` 与草稿。
* **不产生智能体版本、不写注册表、不改任何生产路由。** 它写进 store 的只有一行
  ``kind='trial'`` 的作业行（状态与结果），以及运行目录里的证据 —— 与一次诊断相同，
  但没有任何东西会读它去做路由决策。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES, REGISTERED_TOOL_NAMES, IncidentManifest
from aiops_diagnostics.agent_runner import run_agent_diagnosis
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import Settings
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.platform_paths import reference_root
from aiops_diagnostics.query_scope import resolve_query_scope
from aiops_diagnostics.scope_context import ScopeContext, SubjectRecord

_LOGGER = logging.getLogger("aiops.dify_trial")

#: 草稿提示词的字符上限。它是**运营输入**，进了 run 的初始提示词，所以要有个头。
MAX_DRAFT_PROMPT_CHARS = 20000
#: 草稿可声明的工具数量上限 —— 枚举只有七个，多出来的直接是坏数据。
MAX_DRAFT_TOOLS = len(REGISTERED_TOOL_NAMES)


class TrialRefused(RuntimeError):
    """试运行的输入不可用 —— 拒绝，而不是猜一个默认值跑下去。"""


@dataclass(frozen=True, slots=True)
class TrialDraft:
    """一份草稿：方法论提示词 + 工具白名单。**只能收窄，不能新增。**"""

    prompt: str
    allowed_tools: tuple[str, ...] = field(default=())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TrialDraft:
        """严格解析 —— 多余的字段、未知的工具名、超长提示词都是拒绝。

        ``extra`` 字段一律拒（与请求模型同一纪律）：请求体里出现 ``tenant_id`` /
        ``site_ids`` / ``user_id`` 时，运营以为它们起了作用；它们其实从不参与判定，
        所以最诚实的处理是**让这次调用失败**，而不是安静地忽略。
        """
        unknown = sorted(set(payload) - {"prompt", "allowed_tools"})
        if unknown:
            raise TrialRefused(
                "草稿只接受 prompt 与 allowed_tools；"
                f"这几个字段不参与任何判定，因此不接受：{', '.join(unknown)}"
            )
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise TrialRefused("草稿必须带 prompt")
        if len(prompt) > MAX_DRAFT_PROMPT_CHARS:
            raise TrialRefused(f"草稿 prompt 超过上限 {MAX_DRAFT_PROMPT_CHARS} 字符")

        raw_tools = payload.get("allowed_tools", [])
        if raw_tools is None:
            raw_tools = []
        if not isinstance(raw_tools, list):
            raise TrialRefused("allowed_tools 必须是数组")
        if len(raw_tools) > MAX_DRAFT_TOOLS:
            raise TrialRefused(f"allowed_tools 最多 {MAX_DRAFT_TOOLS} 项")
        names = tuple(str(item) for item in raw_tools)
        bad = sorted({name for name in names if name not in REGISTERED_TOOL_NAMES})
        if bad:
            raise TrialRefused(
                "草稿的工具白名单里出现了未登记的工具："
                + ", ".join(bad)
                + "。草稿只能缩小工具集合，不能新增枚举之外的工具。"
            )
        # 去重保序：勾两次同一个工具不是"两个"，而是同一个。
        deduped = tuple(dict.fromkeys(names))
        return cls(prompt=prompt, allowed_tools=deduped)

    def fingerprint(self) -> str:
        """草稿的摘要哈希 —— 审计里记它，而不是把整段提示词抄进审计。"""
        import hashlib

        digest = hashlib.sha256()
        digest.update(self.prompt.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(",".join(self.allowed_tools).encode("utf-8"))
        return digest.hexdigest()


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
    from aiops_diagnostics.scope_context import (
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


def _blocked_tools(journal: EvidenceJournal) -> tuple[str, ...]:
    """白名单挡掉的工具 —— 从证据日志读，而不是另记一份。

    留痕的唯一位置是执行器写的那条 ``blocked`` 证据（``harness:tool_allowlist``），
    所以这里也只在那一处读。另立一份"被挡清单"就是第二份真相，迟早与执行器不一致。
    """
    names: list[str] = []
    for entry in journal.entries():
        if entry.status == "blocked" and entry.source == "harness:tool_allowlist" and entry.tool not in names:
            names.append(entry.tool)
    return tuple(names)


def run_trial(
    diagnostic_settings: Settings,
    *,
    scope_context: Any,
    order_no: str,
    question: str,
    draft: TrialDraft,
    language: str,
) -> TrialResult:
    """在给定范围里对一张订单跑一次草稿。**没有任何发布副作用。**

    ``scope_context`` 由调用方构造（固定调试租户的 ``admin_context``），这是本函数
    唯一接受的"我是谁"来源 —— 它不从 ``order_no`` 或草稿里推断任何范围。
    """
    if not order_no.strip():
        raise TrialRefused("试运行必须带订单号")
    if set(draft.allowed_tools) - set(ALL_TOOL_NAMES):
        raise TrialRefused("此入口验证旧诊断执行器；aiops-ops 工具请在 Dify 预览中验证")

    # 订单可见性：与 /v1/standard/diagnoses 用**同一条**判据（scope 对订单的可见性），
    # 不是另写一份比较。这里落在签名下面，因为它是唯一能在**进入模型之前**拦住越权的点。
    query_scope = resolve_query_scope(scope_context)

    problem = question.strip() or f"试运行：请对订单 {order_no} 给出诊断"
    request = parse_request(problem, order_no=order_no, tenant_id=scope_context.effective_tenant_id)
    manifest = IncidentManifest.from_request(request)
    selected_provider = diagnostic_settings.agent.select_provider(None)
    workspace = AgentWorkspace.create(
        reference_root(),
        Path(diagnostic_settings.agent.run_root),
        manifest,
        provider_base_url=selected_provider.base_url,
        provider=selected_provider.name,
        key_slot=selected_provider.resolved_key_slot(),
        language=language,
    )
    started_ms = time.monotonic()
    # ``fixture=None``: 试运行打的是**真数据源**，这正是它与"预览"的区别。
    diagnosis = run_agent_diagnosis(
        workspace,
        request,
        diagnostic_settings,
        None,
        provider=selected_provider.name,
        key_slot=selected_provider.resolved_key_slot(),
        scope=query_scope,
        language=language,
        methodology=draft.prompt,
        allowed_tools=draft.allowed_tools,
    )
    journal = EvidenceJournal(workspace, manifest)
    report = diagnosis.model_dump(mode="json")
    _LOGGER.info(
        "dify-trial: order=%s status=%s evidence=%d blocked=%d duration_ms=%d draft=%s",
        order_no,
        diagnosis.status,
        len(diagnosis.evidence_ids),
        len(_blocked_tools(journal)),
        int((time.monotonic() - started_ms) * 1000),
        draft.fingerprint()[:12],
    )
    return TrialResult(
        status=str(diagnosis.status),
        report=report,
        evidence_ids=tuple(diagnosis.evidence_ids),
        blocked_tools=_blocked_tools(journal),
        failed_sources=tuple(diagnosis.failed_sources),
        language=language,
    )


__all__ = [
    "MAX_DRAFT_PROMPT_CHARS",
    "MAX_DRAFT_TOOLS",
    "TrialDraft",
    "TrialRefused",
    "TrialResult",
    "run_trial",
]
