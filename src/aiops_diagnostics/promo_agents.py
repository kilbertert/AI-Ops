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

_AGENT_VERSION_REF = re.compile(r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$")


def promo_intent_from_text(question: str) -> str | None:
    """Deterministic promotional-intent cue match, or None."""
    text = (question or "").strip().lower()
    if not text:
        return None
    for intent, cues in _PROMO_INTENT_CUES.items():
        if any(cue in text for cue in cues):
            return intent
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


def promo_prompt(selection: CustomerAgentSelection, question: str, *, language: str, intent: str) -> str:
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

{search_hint}

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
