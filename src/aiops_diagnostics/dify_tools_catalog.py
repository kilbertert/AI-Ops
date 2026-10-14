"""The seven diagnostic tools as a Dify-selectable catalog（#774 / PRD #772 E）.

Two halves, one subject — *what an operator can select in Dify, and how that
selection reaches our tool allow-list*:

1. **The selection surface.** Dify's tool list shows nothing of ours until an
   API tool provider exists in the workspace, and a provider needs an OpenAPI
   document. :func:`openapi_document` is that document's single source; the
   checked-in copy under ``ops/dify-tools/`` is what an operator pastes into
   Dify, and a test asserts the two agree so the pasted artifact cannot drift
   from the code.

2. **The translation rule.** A DSL tool entry names a provider by its
   ``provider_id`` — and for a *custom API* provider that string is the
   ``ApiToolProvider`` **row UUID** (verified in Dify 1.17.1:
   ``ToolManager.get_api_provider_controller`` looks the provider up by
   ``ApiToolProvider.id == provider_id``). The DSL entry carries **no provider
   name at all** (:class:`AgentToolEntity` is ``provider_type``, ``provider_id``,
   ``tool_name``, …), so "match on ``provider_name == 'aiops'``" — the shape the
   ticket first sketched — has no field to read.

   :func:`partition_aiops_tools` therefore takes the ``aiops`` provider's UUID as
   an **argument**, resolved fresh from the console on every pull by the caller
   (``DifyDslClient.tool_provider_ids``). That is what keeps the promise the
   ticket actually cares about — *rebuilding the Dify instance or the provider
   must not invalidate published mappings*: the UUID never appears in a config
   file, a registry row, or a repository artifact. It is a per-pull lookup, not
   a stored identity.

The catalog is deliberately **subordinate to the executor**: what this resolves
feeds ``AgentConfig.allowed_tools``, which the executor enforces fail-closed
(the default is still "all seven" — ADR-0019 unchanged). Nothing here decides
what a tool *does*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES

#: The API tool provider's name in Dify — fixed by #772 E so an operator cannot
#: rename the surface the pull keys on. It is a *name*, and the pull resolves it
#: to the provider's UUID at run time (module docstring), never the other way.
AI_OPS_PROVIDER_NAME = "aiops"

#: Our tool names, as the set a ``<tool_name>`` must be a member of.
AI_OPS_TOOL_NAMES: frozenset[str] = frozenset(ALL_TOOL_NAMES)

#: What Dify's preview calls get back for every one of the seven operations.
#:
#: It is a **fixed string, not data** — the endpoint reads no data source and
#: creates no run (#772 E: the provider's key is workspace-shared and visible to
#: every admin who can edit it, so a leaked key must buy nothing). And it is
#: also the honest answer to "why did preview not diagnose anything": Dify's own
#: agent loop cannot reach our executor, so a preview result would never reflect
#: production behaviour. The operator's path to a real answer is 试运行.
#: 拼成一行字面量再换行，而不是相邻字符串隐式相接 —— 后者会让「…试运行。」与
#: 「Dify 的…」之间的换行发生在拼接期看不清的地方，`test_i18n` 拦的正是这种拼接
#: （它会把两个词粘成一个，而拼好的值本身看不出问题）。
CATALOG_NOTE = "\n".join(
    (
        "本工具只在 AI-Ops 运行时执行，请使用试运行。",
        "Dify 的「调试与预览」跑的是 Dify 自己的智能体循环，不经过 AI-Ops 的执行器、证据日志与结果校验器，"
        "因此这里的返回不代表生产行为，也不返回任何订单数据。",
        "要按真实数据验证这份草稿，请在编辑页点击「在 AI-Ops 试运行」。",
    )
)

#: 面向运营的每个工具的一句话说明。与 :mod:`aiops_diagnostics.diagnostic_tools`
#: 的 ``TOOL_DESCRIPTIONS`` 分列：那份是给工程师看的数据源（MySQL 表名、TDengine
#: 超级表），这份是给运营看的"这个工具能回答什么"。同一份文案两处用会逼其中一处撒谎。
_TOOL_SUMMARIES: dict[str, str] = {
    "order_snapshot": "订单快照：按订单号取订单主记录（金额、状态、起止时间），是其余六个工具的前置。",
    "fee_snapshot": "费用快照：订单的费用模板与费率记录，用于核对金额与计费口径。",
    "device_snapshot": "设备快照：订单对应充电桩的协议、在线状态、工作状态与故障码。",
    "gun_timeseries": "枪时序：订单时间窗内有界的枪状态与遥测序列。",
    "comm_messages": "通信报文：订单时间窗内有界的解码通信报文。",
    "redis_sync": "Redis 同步：有界的同步流元数据与订单匹配计数，用于同步类问题。",
    "known_runbook": "已知 runbook：既有的确定性诊断报告，作为可引用的先验证据。",
}

#: 后六个工具都要求同一运行里已经有一次成功的 ``order_snapshot``。写在每个
#: description 里而不是写在提供方说明里：运营在 Dify 里逐个勾工具时看的是条目本身。
_DEPENDS_ON_ORDER_SNAPSHOT = "前置：同一次运行中需先成功执行 order_snapshot。"


@dataclass(frozen=True, slots=True)
class DeclaredTool:
    """One ``agent_mode.tools[]`` entry, as Dify reported it.

    ``enabled`` is Dify's own switch: an operator who toggled a tool off expects
    it gone from production, so a disabled entry never enters the allow-list
    (#772 E). It is read even for entries we would refuse anyway, because
    "disabled" and "unknown identity" are different answers for the operator.
    """

    provider_type: str
    provider_id: str
    tool_name: str
    enabled: bool

    @property
    def identity(self) -> str:
        """How this entry is named back to the operator (never how it is keyed)."""
        return f"{self.provider_id}/{self.tool_name}" if self.provider_id else self.tool_name


def openapi_document() -> dict[str, Any]:
    """The OpenAPI 3.0 document an operator pastes into Dify（3A 的说明书）.

    One ``POST`` per tool, ``operationId`` exactly our tool name, and **no
    parameters declared at all** — deliberately, and it is the whole design:
    our tools take their inputs from the run context (ADR-0019), never from the
    model. Leaving the OpenAPI parameters empty is what makes that structural
    rather than a convention: there is nothing for Dify to prompt the model to
    fill in, so an operator cannot pin an order number through a tool argument.
    """
    paths: dict[str, Any] = {}
    for tool in ALL_TOOL_NAMES:
        summary = _TOOL_SUMMARIES[tool]
        description = CATALOG_NOTE if tool == "order_snapshot" else f"{summary}\n{_DEPENDS_ON_ORDER_SNAPSHOT}"
        paths[f"/v1/dify/tools/{tool}"] = {
            "post": {
                "operationId": tool,
                "summary": summary,
                "description": description,
                "responses": {
                    "200": {"description": "固定的说明文本（不返回业务数据）"},
                    "401": {"description": "缺少或错误的提供方密钥"},
                    "404": {"description": "该部署未配置工具说明端点"},
                },
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AI-Ops 诊断工具（仅说明，不执行）",
            "version": "1.0.0",
            "description": (
                "AI-Ops 的七个只读诊断工具目录。这些操作在 Dify 侧只回一段固定说明，"
                "不会执行诊断、不返回任何订单数据；真正的执行发生在 AI-Ops 运行时，"
                "由智能体版本的工具白名单决定。"
            ),
        },
        "servers": [{"url": "https://<AI-Ops 网关域名>"}],
        "paths": paths,
    }


def partition_aiops_tools(
    declared: tuple[DeclaredTool, ...] | list[DeclaredTool],
    *,
    aiops_provider_id: str,
    bindings: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[DeclaredTool, ...]]:
    """Split Dify's declared tools into **our allow-list** and **what we cannot map**.

    Returns ``(allowed_tool_names, unmapped_entries)``. The caller decides what
    an unmapped entry means — the pull path refuses the whole declaration,
    because publishing a subset of what the operator ticked would look like a
    successful pull while their edit silently went missing (the failure the
    knowledge-base bridge already exists to prevent).

    Two identities are recognised, and only two:

    * ``provider_type == "api"`` **and** ``provider_id`` equal to the ``aiops``
      provider's UUID (resolved by the caller, see module docstring) — the entry
      is ours, so ``tool_name`` is one of the seven. A name outside the seven is
      **not** mapped and **not** rejected-but-passed: it is reported as unmapped,
      which is what stops a semantically unknown tool from being mapped in.
    * anything else — resolved through ``bindings``, the explicit
      ``AIOPS_GATEWAY_DIFY_TOOL_BINDINGS`` table. Name equality still never
      implies semantic equality (``time/timezone_conversion`` is a builtin
      Dify tool that is not our ``known_runbook``), so the explicit table stays
      the only door for third-party providers.

    ``enabled == False`` is dropped *before* either rule, and dropping is not
    the same as reporting: the operator turned it off on purpose.
    """
    allowed: list[str] = []
    unmapped: list[DeclaredTool] = []
    for entry in declared:
        if not entry.enabled:
            continue
        mapped: str | None = None
        if entry.provider_type == "api" and aiops_provider_id and entry.provider_id == aiops_provider_id:
            if entry.tool_name in AI_OPS_TOOL_NAMES:
                mapped = entry.tool_name
        else:
            mapped = bindings.get(entry.identity)
        if mapped is None:
            unmapped.append(entry)
            continue
        if mapped not in allowed:
            allowed.append(mapped)
    return tuple(allowed), tuple(unmapped)
