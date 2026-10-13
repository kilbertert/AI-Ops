"""已发布 ``operations`` 智能体版本的选择（#695，PRD #693 的 ② 号路径）。

诊断路径此前完全不读运行时注册表：管家端（``X-Business-Entry: operator``）对一张订单
发起诊断时，跑的是**烤死在代码里**的提示词。本模块把「哪一版服务这个 ``(租户, operator)``」
这件事接到既有机制上——运行时注册表的行点名一个 agent 名，这里按名字取那份**已发布**的
``operations`` 版本。

为什么与 ``select_customer_agent`` 分开写而不是给它加一个 ``agent_type`` 参数：

* 问答路径的语义是**有 KB 才算候选**（``knowledge_base_ids`` 为空不服务 RAG），诊断路径
  的语义是**没有 KB 也能服务**（运维方法论就是提示词本身）。把两个方向相反的谓词塞进
  一个函数的 ``if`` 里，得到的是一个每条分支都要读两遍才敢改的函数。
* 反过来说，把「这一行选出的版本类型对不对」写死在同一处，是本模块唯一的判据：
  两个入口共用同一条注册表，但**类型过滤按入口分叉**——consumer 行选不出 operations，
  operator 行选不出 customer。同名不同入口的漂移方向是放宽，所以它只有一份。

**这个函数只做查找，不做授权**：名字从哪来（注册表行）、调用者有没有权限看到那个租户，
都由调用方决定。名字为 ``None`` 时返回 ``None``——诊断路径**没有**「最新已发布」的兜底，
这与问答路径不同，因为诊断的烤死提示词就是那个兜底，而运维方法论不该被猜测。
"""

from __future__ import annotations

from dataclasses import dataclass

from aiops_diagnostics.agent_lifecycle import AgentNotFound, AgentStore


@dataclass(frozen=True, slots=True)
class OperationsAgentSelection:
    """将服务这次诊断的已发布 ``operations`` 版本。"""

    agent_id: str
    version_no: int
    prompt: str
    #: The published version's tool allow-list. ``None`` = the snapshot
    #: predates the field, which the executor reads as "every tool" — the same
    #: meaning the config default has, so an old snapshot changes nothing.
    allowed_tools: tuple[str, ...] | None = None


def select_operations_agent(
    store: AgentStore,
    tenant_id: str,
    agent_name: str | None,
) -> OperationsAgentSelection | None:
    """返回注册表**点名**的那份已发布 operations 版本，查不到就是 ``None``。

    ``agent_name`` 是运行时注册表对「这个 ``(租户, operator)`` 由谁服务」的回答。
    它是必填的（可为 ``None`` 表示「注册表没给名字」），因为本模块**没有**搜索语义：
    与 ``select_customer_agent`` 的 ``agent_name=None`` 保留「最新已发布」旧规则不同，
    诊断路径的旧规则是烤死提示词，而那是本票要拆掉的东西——回落等于没接线。

    ``None``、找不到、未发布、``agent_type`` 不是 ``operations`` 四种情况**同形**（都返回
    ``None``），调用方据此 fail-closed。它们不区分，是因为区分出来的信息只有「这个租户
    登记没登记」——那是一句关于配置的话，而调用方对它的处置完全相同：不服务。
    """
    if not agent_name:
        return None
    try:
        agents = store.list(tenant_id)
    except Exception:  # store 不可读 —— 与问答路径同向降级为「选不出」
        return None
    for agent in agents:
        if agent.name != agent_name:
            continue
        if agent.status != "published" or agent.published_version is None:
            continue
        try:
            version = store.version(agent.agent_id, tenant_id, agent.published_version)
        except AgentNotFound:
            continue
        snapshot = version.snapshot
        if str(snapshot.get("agent_type")) != "operations":
            continue
        prompt = str(snapshot.get("prompt") or "").strip()
        if not prompt:
            # A published operations agent whose prompt is blank would inject
            # nothing, and "the published version's prompt is injected" would
            # then be satisfied by injecting nothing — the run would be
            # byte-identical to the hard-coded prompt this ticket replaces.
            # The publish gate already requires a non-empty prompt, so this
            # only catches snapshot drift; it fails closed in the same
            # direction as the empty knowledge-base skip in
            # ``select_customer_agent``.
            continue
        published_tools = snapshot.get("allowed_tools")
        return OperationsAgentSelection(
            agent_id=agent.agent_id,
            version_no=version.version_no,
            prompt=prompt,
            allowed_tools=None if published_tools is None else tuple(str(t) for t in published_tools),
        )
    return None
