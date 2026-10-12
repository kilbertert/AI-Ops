"""运维分析的模型轮：只写两段散文（#697，PRD #693 的 ③ 号路径）。

这一轮的输入是**已经算好的数字**，输出只有 ``main_cause`` 与 ``recommendation``。
把它单独成模块（而不是塞进 ``ops_analysis``）是为了让那一侧的依赖保持窄：
``ops_analysis`` 只依赖语言守卫与 i18n，因此它的装配逻辑可以在没有模型、没有 provider、
没有会话的测试里被完整驱动；这里才需要 Codex 会话。

为什么用**零阶工作区**（``AgentWorkspace.create_qa``）：它的 AGENTS.md 已经写明
「这里没有订单元数据、也没有任何订单数据可读」，正是一个只能解释数字、不能去查证据的
回合所需要的护栏。为这一轮另写一份护栏文本会是第二份需要维护的契约。

**但答案形状不同**（#700 在生产上逮到）：零阶那份护栏还写着「回答问题、末尾附提醒语、
返回 ``text``」——这一轮照做了，于是回的是 ``{"text": …}``，而这里要的是
``{"main_cause", "recommendation"}``。护栏与轮次自己的提示词各说各话，正是本模块
第一段记录的同一个失败形态。``answer_contract`` 参数把形状差异交给调用方，护栏其余部分
逐字共用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_workspace import NARRATIVE_ANSWER_CONTRACT, AgentWorkspace
from aiops_diagnostics.codex_runtime import AgentRuntimeError, CodexSession, SDKCodexSession
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, language_name
from aiops_diagnostics.turn_recovery import parse_turn

#: 结构化输出：**两个字符串，没有数字字段**。这是「模型写不出数字」的第一道墙——
#: 不是提示词里的请求，而是它连一个能填数字的位置都没有。第二道墙在装配处
#: （``ops_analysis._narrative`` 只按名字读这两个键）。
OPS_NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "main_cause": {
            "type": "string",
            "description": "对该区间聚合结果的主要成因叙述，使用请求规定的输出语言",
        },
        "recommendation": {
            "type": "string",
            "description": "面向值班工程师的建议动作，使用请求规定的输出语言",
        },
    },
    "required": ["main_cause", "recommendation"],
    "additionalProperties": False,
}

_NARRATIVE_OPENINGS = ("{",)


def ops_narrative_schema() -> dict[str, Any]:
    return OPS_NARRATIVE_SCHEMA


def build_narrative_prompt(facts: dict[str, Any], *, language: str = DEFAULT_LANGUAGE) -> str:
    """把确定性事实渲染成一次「解释它」的请求。

    提示词里明确写出：**不要复述或重算任何数字**。这不是数字安全的那道墙（那道墙在
    装配处），而是为了让模型把篇幅花在解释上——一份把数字抄回来的答案对值班工程师
    没有增量。
    """
    lines = ["以下是一段区间聚合的**确定性结果**（已由后端算出，不需要你复核或重算）："]
    for key in ("failure_rate", "risk", "counts", "secondary", "window", "site_ids", "definition"):
        if key in facts:
            lines.append(f"- {key}: {facts[key]}")
    lines.append("")
    lines.append(
        "请只输出**一个 JSON 对象**，两个字段都是字符串："
        f"`main_cause` 解释这些数字最可能的成因（不要复述数字，也不要编造未给出的数据），"
        f"`recommendation` 给出值班工程师下一步该看什么 / 做什么。"
        f"两段都用{language_name(language)}书写。"
        # #700: the answer *shape* is stated here in the run's own words, not only
        # in AGENTS.md. The model answered this prompt with `main_cause：…` as
        # plain prose twice on 41 — it had read "只输出两段文字" literally, and the
        # workspace guardrail was not enough to make it emit an object. Same rule
        # as the language line above: say it in the turn, not one file away.
    )
    return "\n".join(lines)


def run_narrative_turn(
    facts: dict[str, Any],
    agent_settings: AgentSettings,
    *,
    provider: ProviderConfig | None = None,
    key_slot: str | None = None,
    language: str = DEFAULT_LANGUAGE,
    project_root: Path | None = None,
    session: CodexSession | None = None,
) -> dict[str, Any]:
    """跑一次解释轮，返回解析后的 ``{main_cause, recommendation}``。

    ``session`` 供测试注入；生产路径按零阶工作区建一个真实会话。会话之外的失败
    （provider 不可达、回包不是 JSON）抛 ``AgentRuntimeError``，由调用方把作业
    判成失败——**不会**退化成「没有叙述但有数字」的静默成功，因为那种行的
    ``main_cause`` 为空会让读的人以为聚合本身出了问题。
    """
    root = project_root or Path(__file__).resolve().parents[1]
    selected_provider = agent_settings.select_provider(provider.name if provider else None)
    workspace: AgentWorkspace | None = None
    owned = session is None
    if session is None:
        workspace = AgentWorkspace.create_qa(
            root,
            Path(agent_settings.run_root).expanduser().resolve(),
            provider_base_url=selected_provider.base_url,
            provider=selected_provider.name,
            key_slot=key_slot or selected_provider.resolved_key_slot(),
            language=language,
            # The workspace's default contract says "answer the charging question
            # and end with the reminder line" — which this turn did, returning
            # ``{"text": …}`` that the narrative parser then refused (#700).
            answer_contract=NARRATIVE_ANSWER_CONTRACT,
        )
        session = SDKCodexSession(workspace, agent_settings, provider=selected_provider)
    try:
        # #700: the schema goes to the turn, like every other JSON turn in this
        # runtime (``qa_rag``, the diagnosis coordinator). It was declared in this
        # module and never passed, so the model was free to answer in prose —
        # which is exactly what the first end-to-end run on 41 did, and the job
        # then **hung on `running` forever**: the turn succeeded, the parse
        # raised `AgentRuntimeError`, and the worker's `except (SourceError,
        # ValueError)` did not catch it, so no terminal write ever happened.
        output = session.run(
            build_narrative_prompt(facts, language=language),
            output_schema=ops_narrative_schema(),
        )
        payload = _parse_narrative(output.final_response)
        if payload is None:
            raise AgentRuntimeError("operations-analysis narrative returned invalid JSON")
        return payload
    finally:
        if owned:
            import contextlib

            with contextlib.suppress(Exception):
                session.close()


def _parse_narrative(raw: str) -> dict[str, Any] | None:
    """Take the turn's JSON body, tolerating the transport's known mangling.

    The same ``parse_turn`` the QA and diagnosis lines use: providers wrap JSON
    in fences and occasionally drop the opening characters (measured 5/6 on one
    upstream). None of that tolerance decides what the narrative *says* — it
    only decides whether the text is readable at all — so a shape that cannot be
    recovered is a failure rather than a half-parsed answer.
    """
    parsed = parse_turn(raw, openings=_NARRATIVE_OPENINGS, is_valid=_looks_like_narrative)
    return parsed if isinstance(parsed, dict) else None


def _looks_like_narrative(payload: Any) -> bool:
    return isinstance(payload, dict) and any(key in payload for key in ("main_cause", "recommendation"))
