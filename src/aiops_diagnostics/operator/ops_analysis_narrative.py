"""用一次无工具模型请求解释已算定的聚合；失败不伪装成完整分析。"""

from __future__ import annotations

from typing import Any

from aiops_diagnostics.common.config import AgentSettings, ProviderConfig
from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE, language_name
from aiops_diagnostics.common.model_request import resolve_provider_api_key, structured_request

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


#: Facts the prompt shows the model. A **name list**, not "whatever is in the
#: dict": it was the latter's failure mode that #700's review caught — every
#: card explains its *own* numbers, and the offline / fault-code / payment
#: contracts carry `incidents` / `offline_minutes` / `total_faults` /
#: `top_codes` / `buckets` …, none of which this tuple named. The model then got
#: a risk label and a window and nothing to explain, so a "check this error code
#: first" recommendation could not be grounded in the card's own ranking.
#: `definition` and `reason_code` stay in: they are what tells the model which
#: rule produced the numbers it is describing.
_PROMPT_FACT_KEYS = (
    "card",
    "failure_rate",
    "risk",
    "counts",
    "secondary",
    "incidents",
    "offline_devices",
    "open_incidents",
    "offline_minutes",
    "offline_ratio",
    "total_faults",
    "faulty_devices",
    "top_codes",
    "faulty_ratio",
    "total",
    "paid",
    "paid_refunded",
    "paid_net",
    "unpaid",
    "refund_share",
    "devices_total",
    "window",
    "site_ids",
    "definition",
    "reason_code",
)


#: ``None`` means "the surface could not compute this one". Sending it as a fact
#: would invite the model to explain an absence as if it were a measured value —
#: the same false claim ``reason_code`` exists to prevent.
def _fact_lines(facts: dict[str, Any]) -> list[str]:
    return [f"- {key}: {facts[key]}" for key in _PROMPT_FACT_KEYS if key in facts and facts[key] is not None]


def build_narrative_prompt(facts: dict[str, Any], *, language: str = DEFAULT_LANGUAGE) -> str:
    """把确定性事实渲染成一次「解释它」的请求。

    提示词里明确写出：**不要复述或重算任何数字**。这不是数字安全的那道墙（那道墙在
    装配处），而是为了让模型把篇幅花在解释上——一份把数字抄回来的答案对值班工程师
    没有增量。
    """
    lines = ["以下是一段区间聚合的**确定性结果**（已由后端算出，不需要你复核或重算）："]
    lines.extend(_fact_lines(facts))
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
) -> dict[str, Any]:
    selected = provider or agent_settings.select_provider(None)
    payload = structured_request(
        provider=selected,
        api_key=resolve_provider_api_key(agent_settings, provider=selected, key_slot=key_slot),
        messages=[{"role": "user", "content": build_narrative_prompt(facts, language=language)}],
        name="operations_narrative",
        schema=OPS_NARRATIVE_SCHEMA,
    )
    if not isinstance(payload, dict) or set(payload) != {"main_cause", "recommendation"}:
        raise RuntimeError("invalid operations narrative")
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in payload.values()
    ):
        raise RuntimeError("invalid operations narrative")
    return payload
