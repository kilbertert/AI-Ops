"""把「本人订单状态」交给客户执行节点跑，并把它接进**现有 App 聊天**。

## 为什么需要这一层

`POST /v1/orders/{order_no}/charging-status/delegated` 已经能把一次读取交给节点，但它
是**同步**的：调用方一直等到平台派发、节点跑完整轮、结果回到网关。这条路对「App 聊天」
不成立 —— 用户的 POST 会挂住整轮节点时间，前端早就超时了。

而 `_consumer_order_chat` 今天是**确定性直读**：网关自己读 SQL + CPMS 并当场作答，
与节点无关。

本模块把两件事接起来，同时**不复制第二套逻辑**：

* `resolve_binding` / `issue_run` / `dispatch_run` / `settled_run` 是**从既有委派入口抽出来
  的共享操作**。两个入口都调它，授权、TTL、执行配置守卫、完成判据只有一份实现。
* 聊天入口只多做一件事：**先建 QA、立刻返回**，把「等节点」搬到既有 QA worker 里。

## 不变量（本模块存在的理由）

1. **默认关闭**。没有绑定文件、或绑定里没有这一场景时，聊天入口走**原分支**，
   行为与接线前逐字一致。
2. **命中绑定后失败就是失败**。节点失败、超时、平台不可用、配置漂移、执行配置未被核对 ——
   全部落到 QA 的 `failed` 终态。**不回落直读、不回落旧模型、不换别的节点。**
   回落到任何一条都在把「另一次读取」冒充成本次结果。
3. **答案只来自同一条 run 的 succeeded 快照**。读取路径按 QA 反查 run（存储关联），
   再复用既有的返回前放行判据；不另读 CPMS 去「补」一个快照。
4. **过期、撤权、停用、取消都拒绝正文**。TTL 由 `agent_runs` 自己管，不因为轮询而延长；
   取消先落终态并撤销已关联的 run。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_runs import (
    SUCCEEDED,
    AgentRun,
    AgentRunConfig,
    AgentRunConfigError,
    AgentRunDispatchError,
    SnailDispatcher,
)
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.scope_context import ScopeContext

#: QA 的 ``internal_run_id`` 前缀。它**只是**给人看的标签；
#: 「哪条 run 承载哪个 QA」由 `agent_runs.qa_id` 这一列决定，不从字符串反推。
CHAT_RUN_PREFIX = "consumer-order-chat-node:"


class ChatRunUnavailable(RuntimeError):
    """本场景不能走节点。``code`` 是回给调用方的稳定错误码。

    与 `AgentRunDispatchError` 的区别：后者是「平台那一跳失败」，这个是「这一条请求
    不该/不能交给节点」。两者在聊天入口都**落到失败终态**，不回落。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """可信服务端的绑定：这个场景允许走节点，且用的是哪个配置/清单。

    只由服务端配置决定。客户端、模型、请求体都不能影响其中任何一项。
    """

    config_file: str
    manifest_file: str

    @property
    def enabled(self) -> bool:
        return bool(self.config_file)


def resolve_binding(settings: GatewayServerSettings) -> ChatBinding:
    """读出本场景的绑定。**未配置就是未启用**，不猜、不从别的开关推导。"""
    return ChatBinding(
        config_file=str(getattr(settings, "chat_execution_config_file", "") or ""),
        manifest_file=str(getattr(settings, "chat_execution_manifest_file", "") or ""),
    )


def issue_for_store(
    *,
    store: Any,
    settings: GatewayServerSettings,
    caller: ScopeContext,
    order_no: str,
    gateway_version: str,
) -> tuple[AgentRun, AgentRunConfig]:
    """签发 + 返回本次使用的配置（派发需要它）。"""
    from aiops_diagnostics import execution_manifest

    binding = resolve_binding(settings)
    try:
        config = AgentRunConfig.load(Path(binding.config_file))
    except AgentRunConfigError as exc:
        raise ChatRunUnavailable(
            "AGENT_RUN_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True
        ) from exc
    if config.environment != settings.execution_environment:
        raise ChatRunUnavailable("AGENT_RUN_ENVIRONMENT_MISMATCH", "agent run unavailable", retryable=True)
    try:
        manifest = execution_manifest.load(binding.manifest_file)
    except execution_manifest.ExecutionManifestError as exc:
        raise ChatRunUnavailable("EXEC_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True) from exc
    run = store.issue(
        scope=caller,
        order_no=order_no,
        config=config,
        gateway_version=gateway_version,
        expected_agent_version=(manifest.identity if manifest is not None else None),
    )
    return run, config


def dispatch_run(*, store: Any, config: AgentRunConfig, run: AgentRun, dispatcher: Any = None) -> None:
    """派发一次。失败把记录钉成 failed 并抛 `ChatRunUnavailable`。"""
    try:
        (dispatcher or SnailDispatcher()).dispatch(config, run.run_id)
    except AgentRunDispatchError as exc:
        store.fail(run.run_id, "platform_unavailable")
        raise ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable") from exc


def settled_run(*, store: Any, run_id: str) -> AgentRun:
    """等一条已经派发的 run 落定，并断言它真的成功了。

    「平台回了话」不等于「节点跑完了」—— 这是 M3 那一片的核心教训，这里沿用同一判据。
    """
    try:
        settled = store.get(run_id)
    except (ValueError, TypeError) as exc:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result") from exc
    if settled is None:
        raise ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable", retryable=True)
    if settled.state != SUCCEEDED:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result")
    return settled


def answer_from_run(run: AgentRun, *, language: str) -> dict[str, Any]:
    """把该 run 的**成功快照**适配成原回答格式。不读第二个来源。

    快照的字段集合与确定性直读那条路一致，所以渲染复用同一个 `status_answer` ——
    App 看到的 blocks 形状不会因为「谁来读」而变。
    """
    from aiops_diagnostics.consumer_order_chat import status_answer

    if run.snapshot is None:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result")
    return status_answer(run.snapshot, language)


__all__ = [
    "CHAT_RUN_PREFIX",
    "ChatBinding",
    "ChatRunUnavailable",
    "answer_from_run",
    "dispatch_run",
    "issue_for_store",
    "resolve_binding",
    "settled_run",
]
