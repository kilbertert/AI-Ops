"""Promotional case/solution routing (#231).

`case_exploration` and `solution_discovery` run through the current tenant's
published promotional Agent + knowledge bases — never the customer-service FAQ
agent. A tenant shortcut override may pin one immutable published version;
platform defaults never carry a tenant-owned target. Free-text questions reach
the same path via named-intent cues or the lightweight classifier.

The promotional knowledge base is the single source of truth for card facts.
An empty retrieval honestly returns "no available case/solution".
"""

from __future__ import annotations

import re
from typing import Any

from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    PROMO_EMPTY_MESSAGES,
    PROMO_UNAVAILABLE_MESSAGES,
    language_name,
)
from aiops_diagnostics.qa_rag import CustomerAgentSelection

PROMO_INTENTS = frozenset({"case_exploration", "solution_discovery"})

# Industry/scenario keywords steer WHICH case to look for, never the card facts.
_PROMO_SCENARIO_CUES = (
    "港口",
    "码头",
    "重卡",
    "卡车",
    "车队",
    "物流",
    "协议",
    "充电协议",
    "运营商",
    "场站",
    "公交",
    "巴士",
    "矿区",
    "港口岸电",
)

# Narrow cues that NAME the promotional intents. Bare "方案"/"solution" is too
# broad and would steal ordinary support questions.
_PROMO_INTENT_CUES = {
    "case_exploration": (
        "客户案例",
        "案例库",
        "标杆",
        "success story",
        "customer case",
        "case study",
        "case studies",
    ),
    "solution_discovery": ("行业方案", "行业解决方案", "解决方案", "industry solution"),
}

#: 「说出意图的**词**」之外的第二种形状：**结构**（#413）。
#:
#: 只堆词表有一个可证的漏网面 —— `重卡充电案例` 缺的正是「客户」两个字，而用户已经
#: 说清了他要**案例**（实测：2026-09-24，41 上落到 `consumer.faq.q009`「RFID 卡怎么
#: 绑定」，用户想看案例却拿到操作说明）。补一条 `重卡充电案例` 只能挡住这一条，
#: 下一个说法照样漏。
#:
#: 但也不能退回裸 `案例`/`方案`：那个方向已经被实测证伪 —— #408 期间**曾**把
#: `case_exploration`/`solution_discovery` 加进 FAQ 抑制集合，代价是模型把**目录自己的
#: 标题**里的 "Guide"、  "SOP" 读成宣传，给 q010 的标题加个 "Please show me the "
#: 就变成宣传卡片（见 `docs/validation.md` 的「八之三」与取舍 2）。
#:
#: ⇒ 取两者之间：**「案例」可以裸出现**（实测 45 条目录问题 + 315 个标题变体、86 条真实
#: 语料里**没有一条**支持类问题用它），而**「方案」必须带场景词**（`我的充电方案是什么`
#: 就是反例：它在问自己的充电安排，不是要行业方案）。
#:
#: 中文里「X案例」是「案例」的常规写法（`重卡充电案例`、`新加坡无人电动巴士案例`），
#: 因此裸「案例」不是原注释担心的那种宽泛词 —— 原注释把两者放在一起是过度概括。
_BARE_INTENT_CUES = {"case_exploration": ("案例",)}

#: 「场景词 + 方案」= 行业方案。场景词表**复用** `_PROMO_SCENARIO_CUES`
#: （它本来就在描述行业场景，另立一份只会漂移）。
_SCENARIO_SOLUTION_WORD = "方案"
_SCENARIO_SOLUTION_INTENT = "solution_discovery"

#: 🔴 **领地标记**：方案属于**说话人自己**时，它在问自己的安排，不是要行业方案。
#:
#: 场景词不够 —— `我的**重卡**充电方案是什么` 里「重卡」照样满足场景条件，而它问的是
#: 我这一台车怎么充电（评审指出的反例，与 `我的充电方案是什么` 同形）。
#: 判据取「方案」**之前**是否出现过「我的」：行业方案的说法里不会有它
#: （`港口集卡方案`、`有没有重卡充电的方案`），而个人安排的说法里几乎必然有。
_POSSESSIVE_MARKERS = ("我的",)
_SCENARIO_SOLUTION_EXCLUSIONS = _POSSESSIVE_MARKERS

_AGENT_VERSION_REF = re.compile(r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$")


def _is_personal_solution_wording(text: str, word: str) -> bool:
    """「方案」前是否出现了领地标记（`我的`）⇒ 说的是自己的安排，不是行业方案。"""
    index = text.find(word)
    if index <= 0:
        return False
    head = text[:index]
    return any(marker in head for marker in _SCENARIO_SOLUTION_EXCLUSIONS)


def promo_intent_from_text(question: str) -> str | None:
    """Deterministic promotional-intent cue match, or None.

    两条判据，**先后有序**：先词表（精确说出意图），再结构（#413）。
    顺序不影响结果（两条都不重叠），但写死顺序是为了让后来者一眼看出谁是主判据。
    """
    text = (question or "").strip().lower()
    if not text:
        return None
    for intent, cues in _PROMO_INTENT_CUES.items():
        if any(cue in text for cue in cues):
            return intent
    # 结构判据一：中文「X案例」——「案例」单独出现即是案例意图。
    for intent, cues in _BARE_INTENT_CUES.items():
        if any(cue in text for cue in cues):
            return intent
    # 结构判据二：场景词 + 「方案」，且**方案不是说话人自己的**。
    # 两条缺一不可，所以不能并进上面的裸词表。
    if (
        _SCENARIO_SOLUTION_WORD in text
        and any(scenario.lower() in text for scenario in _PROMO_SCENARIO_CUES)
        and not _is_personal_solution_wording(text, _SCENARIO_SOLUTION_WORD)
    ):
        return _SCENARIO_SOLUTION_INTENT
    return None


def scenario_keywords(question: str) -> list[str]:
    """Industry keywords found in the question (steers the KB search)."""
    return [cue for cue in _PROMO_SCENARIO_CUES if cue in (question or "")]


def select_promo_agent(
    store: Any,
    tenant_id: str,
    target_agent_version: str | None,
) -> CustomerAgentSelection | None:
    """Resolve ``agt_xxx#vN`` to a published blocks-v1 snapshot, or None."""
    if not target_agent_version or not _AGENT_VERSION_REF.fullmatch(target_agent_version):
        return None
    agent_id, _, version_no = target_agent_version.partition("#v")
    try:
        version = store.version(agent_id, tenant_id, int(version_no))
    except Exception:  # noqa: BLE001 - absent/stale target degrades honestly
        return None
    snapshot = version.snapshot
    if str(snapshot.get("status")) != "published":
        return None
    if str(snapshot.get("agent_type")) != "customer":
        return None
    if str(snapshot.get("output_contract") or "blocks-v1") != "blocks-v1":
        return None
    kb_ids = tuple(str(item) for item in (snapshot.get("knowledge_base_ids") or ()) if str(item).strip())
    if not kb_ids:
        return None
    return CustomerAgentSelection(
        agent_id=agent_id,
        version_no=version.version_no,
        prompt=str(snapshot.get("prompt") or ""),
        knowledge_base_ids=kb_ids,
    )


def promo_empty_result(
    language: str, intent: str, *, retrieval_status: str = "not_found"
) -> dict[str, str | list[dict[str, str]]]:
    """Structured card substitute when no promotional card can be produced.

    ``retrieval_status`` distinguishes two different situations that used to
    share the same copy:

    - ``not_found`` — a search really ran and returned nothing.
    - ``unavailable`` — no search ran at all: the action has no resolvable
      promotional agent, the dependency is down, or the library could not be
      reached.

    Claiming "no matching material" in the second case is a statement about a
    library nobody read, which is what 41 showed for solution_discovery (a
    shortcut with no target, so no query was ever issued).
    """
    source = PROMO_UNAVAILABLE_MESSAGES if retrieval_status == "unavailable" else PROMO_EMPTY_MESSAGES
    pack = source.get(language) or source[DEFAULT_LANGUAGE]
    key = intent if intent in pack else "case_exploration"
    return {
        "blocks": [{"kind": "text", "text": pack[key]}],
        "retrieval_status": retrieval_status,
    }


def promo_prompt(
    selection: CustomerAgentSelection,
    question: str,
    *,
    language: str,
    intent: str,
    history: str = "",
) -> str:
    """Initial prompt for the promotional card run (same harness as customer QA)."""
    card = "customer case card" if intent == "case_exploration" else "industry solution card"
    topic = "customer cases" if intent == "case_exploration" else "industry solutions"
    cues = scenario_keywords(question)
    if cues:
        search_hint = (
            "Industry keywords present in the request (include them in knowledge_search): "
            + ", ".join(cues)
            + "."
        )
    else:
        search_hint = (
            "No industry keyword was named; search the promotional library for a "
            "representative case/solution. Rotate fairly. Do not invent a customer."
        )
    return f"""You are the published promotional agent defined below. The user
wants to explore {topic}.
Produce a {card} with four parts, one text block each, in this order: the case
title; the industry pain points; the solution; and the commercial results or
benchmark significance.

Write each part's heading in the output language. Do NOT copy a heading from
these instructions — they name the parts in English only so the structure is
unambiguous, and copying them would leave an English heading in a card written
in another language. The part names above describe what to write, not the
words to use.

Promotional agent instructions (authoritative for tone and scope):
\"\"\"{selection.prompt}\"\"\"

User request (may arrive in any language):
\"\"\"{question}\"\"\"

{history}{search_hint}

Hard rules:
- Every customer fact, number, and named site in the card MUST come from
  knowledge_search chunks returned this turn. If retrieval found nothing,
  answer honestly that no matching case/solution material is available —
  NEVER invent a customer, site, or result.
- You have ONE bounded tool: `knowledge_search`. Request it with a focused
  query before answering. `query` is REQUIRED and must be a SHORT keyword
  phrase (3-8 words, e.g. `新加坡 无人电动巴士` or `port charging case`) —
  never a full sentence, never the card structure words. Keep `reason`
  to one brief sentence; a long `reason` pollutes retrieval and the
  search will miss.
- Do not access orders, accounts, or any business system. This is a
  promotional content request only.

Output language: every customer-facing `text` block MUST be written in
{language_name(language)}. Return the structured turn contract:
`kind=tool_requests` (one knowledge_search) or `kind=answer` with `blocks[]`
and the honest `retrieval_status`. Media and reference blocks may ONLY cite
ids returned by this turn's searches.
"""
