"""Dify DSL 文档解析、版本校验与 AgentConfig/Workflow 映射规则。

纯转换与只读解析层：仅依赖 ``dify_dsl_client`` 的异常与客户端接口，
负责将 Dify 导出的 YAML/DSL 映射为本地 ``AgentConfig`` 或规范化 ``workflow_content``。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import yaml

from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslClient,
    DifyDslMalformed,
    DifyDslVersionUnsupported,
)
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig

#: version bump fails loudly here and the mapping gets read again.
SUPPORTED_DSL_MAJOR = 0
SUPPORTED_DSL_MINOR = 7
_DSL_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def map_dsl_to_config(
    dsl_text: str,
    *,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    dataset_resolver: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
) -> AgentConfig:
    """Map exported DSL text onto the ``AgentConfig`` fields we run on.

    Every field we do read is read explicitly, and a field that is absent or
    holds a value we cannot use raises instead of being defaulted — an app we
    cannot map is never silently half-imported. ``opening_statement`` and
    ``suggested_questions`` are not read at all; see the module docstring.

    ``knowledge_base_ids`` is the caller's, like ``agent_type``: the DSL's
    dataset ids are Dify's, not ours (module docstring). ``None`` means the
    caller did not state a binding, and a DSL that *does* declare one then
    fails loudly — the alternative is publishing a Dify UUID as a knowledge
    base. An explicit tuple (including ``()``) is taken as stated.

    ``dataset_resolver`` is how a declared dataset stops being a dead end: the
    caller injects a function that turns Dify's dataset id into **our**
    knowledge-base ids (via the operator's handle — see
    :meth:`DifyDslClient.dataset_knowledge_id`). Kept as a parameter rather
    than a call from here so this function stays pure and testable with no
    network. ``None`` keeps the old behaviour exactly: a declared dataset with
    nowhere to resolve it is a hard failure, never a silent copy.
    """
    try:
        document = yaml.safe_load(dsl_text)
    except yaml.YAMLError as exc:
        raise DifyDslMalformed(f"DSL is not valid YAML: {exc.__class__.__name__}") from exc
    if not isinstance(document, dict):
        raise DifyDslMalformed("DSL root must be an object")

    _require_supported_version(document.get("version"))
    kind = document.get("kind")
    if kind not in {None, "app"}:
        raise DifyDslMalformed(f"DSL kind must be 'app', got {kind!r}")

    model_config = document.get("model_config")
    if not isinstance(model_config, dict):
        raise DifyDslMalformed("DSL has no model_config object (only chat/completion apps carry one)")

    declared = _declared_dataset_ids(model_config)
    if knowledge_base_ids is None:
        if declared and dataset_resolver is not None:
            knowledge_base_ids = dataset_resolver(declared)
        elif declared:
            raise DifyDslMalformed(
                "DSL 声明了知识库数据集（"
                + ", ".join(declared)
                + "），但那是 Dify 的 dataset id，不是我们的知识库 id —— 两者不是同一个 id 空间。"
                "请显式给出 knowledge_base_ids（`admin pull-dify --knowledge-base-id`），不要按字面搬运。"
            )
        else:
            knowledge_base_ids = ()

    # 旧 Python 工具不再执行；原生工作流/节点清单单独发布。
    declared_tools = _declared_tools(model_config)
    if any(entry.enabled for entry in declared_tools):
        raise DifyDslMalformed("Python 内置工具已退役，请发布 Dify 工作流或 Java 节点清单")

    return AgentConfig(
        agent_type=agent_type,
        prompt=_required_prompt(model_config),
        knowledge_base_ids=tuple(knowledge_base_ids),
        model=_required_model(model_config),
        output_contract=output_contract or _default_output_contract(agent_type),
        # #585 的反转（PR-5）：`opening_statement` 现在**被读取**。它只覆盖 zh ——
        # 其余语言由制品回答（运行时没有翻译机制）。空/缺失 = 内层默认 ""，
        # 服务端回退制品，与新增该字段前的行为逐字相同。
        opening_statement=_optional_opening_statement(model_config),
        suggested_questions=_optional_suggested_questions(model_config),
        allowed_tools=(),
    )


def _resolve_declared_datasets(
    dataset_ids: tuple[str, ...],
    registry: Mapping[str, Any],
    client: DifyDslClient,
    *,
    tenant_id: str = "",
) -> tuple[str, ...]:
    """Translate Dify dataset ids into **our** knowledge-base ids, or refuse.

    Two hops, and every failure is a refusal rather than a fallback:

    * dataset id → the operator's handle (``external_knowledge_id``), read from
      the console. A **native** Dify dataset has no handle, so it is a refusal:
      there is nothing of ours behind it.
    * handle → our knowledge-base ids, through the same registry the inbound
      retrieval route uses. An unregistered handle is a refusal: we never guess
      which tenant a dataset belongs to.

    ``tenant_id`` is checked when given: a registry row for **another tenant**
    is refused rather than bound. The registry is a global table, and without
    this a dataset pointed at the wrong operator's handle would quietly attach
    that operator's knowledge base to this agent.

    All-or-nothing on purpose. Accepting three of four declared datasets would
    look like a successful pull while an operator's edit silently went missing,
    which is the failure this whole change exists to remove.

    Typed against ``Any`` rather than ``gateway_config.DifyKnowledgeBinding``
    so this module does not depend on the server-config module: it only ever
    reads ``knowledge_base_ids`` and ``tenant_id`` off the row.
    """
    resolved: list[str] = []
    for dataset_id in dataset_ids:
        handle = client.dataset_knowledge_id(dataset_id)
        binding = registry.get(handle)
        if binding is None:
            raise DifyDslMalformed(
                f"Dify dataset {dataset_id} 的外接知识库句柄是 {handle!r}，但 "
                "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS 里没有它 —— 我们不知道它对应哪个"
                "租户、哪些知识库。请先在 41 上登记这个句柄，或在 Dify 里改挂已登记的那一个。"
            )
        if tenant_id and str(getattr(binding, "tenant_id", "")) != tenant_id:
            raise DifyDslMalformed(
                f"Dify dataset {dataset_id} 的句柄 {handle!r} 登记在另一个租户名下"
                f"（{getattr(binding, 'tenant_id', '')}），而这个 agent 属于 {tenant_id} —— "
                "拒绝把别的租户的知识库挂上来。"
            )
        for knowledge_base_id in binding.knowledge_base_ids:
            if knowledge_base_id not in resolved:
                resolved.append(knowledge_base_id)
    return tuple(resolved)


def _require_supported_version(value: Any) -> None:
    if not isinstance(value, str):
        raise DifyDslMalformed(f"DSL has no version string, got {value!r}")
    match = _DSL_VERSION.match(value.strip())
    if not match:
        raise DifyDslMalformed(f"DSL version is not a semantic version: {value!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) != (SUPPORTED_DSL_MAJOR, SUPPORTED_DSL_MINOR):
        raise DifyDslVersionUnsupported(
            f"DSL version {value!r} is outside the supported "
            f"{SUPPORTED_DSL_MAJOR}.{SUPPORTED_DSL_MINOR}.x shape; "
            "re-read the mapping before pulling from this Dify version"
        )


def _required_prompt(model_config: dict[str, Any]) -> str:
    prompt = model_config.get("pre_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise DifyDslMalformed("DSL model_config.pre_prompt is empty; the prompt is the reason to pull")
    return prompt


def _required_model(model_config: dict[str, Any]) -> str:
    model = model_config.get("model")
    if not isinstance(model, dict):
        raise DifyDslMalformed(f"DSL model_config.model must be an object, got {type(model).__name__}")
    name = model.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DifyDslMalformed("DSL model_config.model.name is empty")
    return name


def _declared_dataset_ids(model_config: dict[str, Any]) -> tuple[str, ...]:
    """Read the enabled **Dify** dataset ids out of ``dataset_configs``.

    These are reported, never assigned: see the module docstring on why a Dify
    dataset id must not become one of our ``knowledge_base_ids``. Callers use
    this to say "the app declares knowledge, but you have not told me which of
    ours that is" instead of silently carrying the wrong id space forward.

    Absent means "no knowledge attached", which is a real state — both fixtures
    pulled from 36 carry exactly that. A *present but unreadable* value raises
    instead: treating it as empty would quietly lose a binding the operator
    configured. (Entry shape: flat ``{enabled, id}``, measured on Dify 1.17.1 —
    see the loop below.)

    Note for whoever reviews a draft: Dify's chat frontend replaces
    ``dataset_configs`` wholesale, so a change made in that form keeps only
    ``retrieval_model`` and loses the ``datasets`` subtree. A later pull then
    sees no knowledge declared, which this reports honestly as empty.
    """
    dataset_configs = model_config.get("dataset_configs")
    if dataset_configs is None:
        return ()
    if not isinstance(dataset_configs, dict):
        raise DifyDslMalformed("DSL model_config.dataset_configs must be an object")
    datasets = dataset_configs.get("datasets")
    if datasets is None:
        return ()
    if not isinstance(datasets, dict):
        raise DifyDslMalformed("DSL model_config.dataset_configs.datasets must be an object")
    entries = datasets.get("datasets")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise DifyDslMalformed("DSL dataset_configs.datasets.datasets must be a list")

    ids: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DifyDslMalformed(f"dataset entry {index} must be an object, got {type(entry).__name__}")
        # 形状**实测**自 Dify 1.17.1 的 `GET …/export`：条目是**平铺**的
        # `{"enabled": true, "id": "<uuid>"}`，**没有**嵌套的 `dataset` 对象。
        # 旧实现要求嵌套 ⇒ 对每一个真的挂了知识库的 app 都报「must carry a 'dataset'
        # object」。它能活到现在只是因为**从来没有一个 app 真的挂过**（两个 fixture 都
        # 连 `datasets` 键都没有），所以那条分支一次都没被走到。
        if not entry.get("enabled", False):
            continue
        dataset_id = entry.get("id")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise DifyDslMalformed(f"enabled dataset entry {index} has no id")
        ids.append(dataset_id)
    return tuple(ids)


def _optional_suggested_questions(model_config: dict[str, Any]) -> tuple[str, ...]:
    """Dify 的 ``suggested_questions``，缺失/None 时是空元组（= 不覆盖）。

    与开场白同一条纪律：不可读的值**抛错**，不静默当成"没给"。条目里的空串被跳过
    （Dify 的空行不是一条问题），但**整个字段类型不对**仍然抛。
    """
    value = model_config.get("suggested_questions")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DifyDslMalformed(
            f"DSL model_config.suggested_questions must be a list, got {type(value).__name__}"
        )
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise DifyDslMalformed(
                f"DSL model_config.suggested_questions[{index}] must be a string, got {type(item).__name__}"
            )
        if item.strip():
            out.append(item.strip())
    return tuple(out)


def _optional_opening_statement(model_config: dict[str, Any]) -> str:
    """Dify 的 ``opening_statement``，缺失/空交付时是空串（= 不覆盖）。

    ``None`` 是一个 Dify 会**写出来**的值（新建 app 时它是 null），而它和空串在这里
    同义：这个 app 没给开场白。**一个不可读的值仍然抛错** —— 静默当成"没给"会让
    运营写进去的开场白消失且没有任何信号。
    """
    value = model_config.get("opening_statement")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DifyDslMalformed(
            f"DSL model_config.opening_statement must be a string, got {type(value).__name__}"
        )
    return value.strip()


def _declared_tools(model_config: dict[str, Any]) -> tuple[Any, ...]:
    """The declared ``agent_mode.tools`` as :class:`DeclaredTool`, or refuse.

    Same contract as :func:`_declared_dataset_ids` — read as **reported**, never
    assigned — but it keeps the fields the 3A rule keys on (``provider_type``,
    ``provider_id``, ``enabled``) instead of flattening them to
    ``provider_id/tool_name``. 3A matches the provider by **UUID**, and a string
    that has been joined cannot be un-joined reliably (a UUID never contains
    ``/``, but the flattening is what made the first version of this rule
    unimplementable, so it is not repeated).
    """
    from aiops_diagnostics.dify.dify_tools_catalog import DeclaredTool

    agent_mode = model_config.get("agent_mode")
    if agent_mode is None:
        return ()
    if not isinstance(agent_mode, dict):
        raise DifyDslMalformed("DSL model_config.agent_mode must be an object")
    tools = agent_mode.get("tools")
    if tools is None:
        return ()
    if not isinstance(tools, list):
        raise DifyDslMalformed("DSL model_config.agent_mode.tools must be a list")

    entries: list[DeclaredTool] = []
    for index, entry in enumerate(tools):
        if not isinstance(entry, dict):
            raise DifyDslMalformed(f"tool entry {index} must be an object, got {type(entry).__name__}")
        tool_name = entry.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise DifyDslMalformed(f"tool entry {index} has no tool_name")
        provider_id = entry.get("provider_id")
        entries.append(
            DeclaredTool(
                provider_type=str(entry.get("provider_type") or ""),
                provider_id=provider_id.strip() if isinstance(provider_id, str) else "",
                tool_name=tool_name.strip(),
                # Dify 的开关缺省为「开」：没写 enabled 的条目是有效条目，
                # 而不是一个需要运营去补字段的残缺声明。
                enabled=entry.get("enabled", True) is not False,
            )
        )
    return tuple(entries)


def _default_output_contract(agent_type: str) -> str:
    from aiops_diagnostics.publication.agent_manifest import DEFAULT_OUTPUT_CONTRACTS

    try:
        return DEFAULT_OUTPUT_CONTRACTS[agent_type]
    except KeyError as exc:
        raise DifyDslMalformed(f"agent_type {agent_type!r} has no default output contract") from exc


def _dsl_kind(dsl_text: str) -> str:
    """这份 DSL 是哪种应用：`workflow` / `chat` / `completion`。

    `map_dsl_to_config` 只吃 chat/completion（它们有 `model_config`）。workflow 应用的
    内容在 `workflow.graph` 里，**没有** `model_config` —— 拿它去走那条映射会得到
    `DSL has no model_config object`，而那看起来像"DSL 坏了"，其实是"这是另一种应用"。
    """
    try:
        document = yaml.safe_load(dsl_text)
    except Exception as exc:  # noqa: BLE001 - YAML 解析失败就是坏 DSL
        raise DifyDslMalformed(f"DSL is not valid YAML: {type(exc).__name__}") from exc
    if not isinstance(document, dict):
        raise DifyDslMalformed("DSL root must be an object")
    app = document.get("app")
    mode = app.get("mode") if isinstance(app, dict) else None
    if isinstance(mode, str) and mode.strip():
        return mode.strip()
    # 没有 `app.mode` 时退回看形状：有 `workflow.graph` 就是 workflow。
    return "workflow" if isinstance(document.get("workflow"), dict) else "chat"


def _merge_workflow_snapshot(previous: dict, workflow_body: dict, *, name: str) -> dict:
    """把 workflow 内容**叠加**到既有已发布快照上，而不是用它替换。

    这个 agent 可能同时在给客服问答（`qa_rag.select_customer_agent`）或运营诊断
    （`ops_agents`）供配置，它们从**已发布快照**里读 `agent_type` / `prompt` /
    `knowledge_base_ids`，并先按 `agent_type` 筛选。用一份只含 workflow 的快照覆盖，
    那些入口会当场选不到这个 agent —— 发布一条订单状态路由，顺手停掉了同一个绑定上
    的其他业务入口。
    """
    base = dict(previous) if isinstance(previous, dict) else {}
    return {**base, **workflow_body, "name": name}


def workflow_content(dsl_text: str) -> dict[str, Any]:
    """保留工作流行为及变量，剔除画布布局与选择状态。"""
    document = yaml.safe_load(dsl_text)
    workflow = document.get("workflow") if isinstance(document, dict) else None
    if not isinstance(workflow, dict) or not isinstance(workflow.get("graph"), dict):
        raise DifyDslMalformed("workflow DSL has no graph")
    graph = workflow["graph"]
    return {
        **{k: v for k, v in workflow.items() if k != "graph"},
        "graph": {
            "nodes": [
                {
                    "id": node.get("id"),
                    "data": {k: v for k, v in node.get("data", {}).items() if k != "selected"},
                }
                for node in graph.get("nodes", [])
            ],
            "edges": [
                {
                    k: v
                    for k, v in edge.items()
                    if k in {"source", "target", "sourceHandle", "targetHandle", "data"}
                }
                for edge in graph.get("edges", [])
            ],
        },
    }
