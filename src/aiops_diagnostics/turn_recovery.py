"""Repair for a streamed model turn that lost the head of its JSON body.

The provider behind the Responses API relays an answer as SSE, and the first
``output_text`` delta carrying the opening of the JSON body is intermittently
dropped in flight. Measured against the live endpoint (2026-09-22, 6 runs of one
fixed prompt): 5 of 6 turns arrived with the head missing and the tail intact,
losing between 2 and 18 characters — always a prefix of the body, never
anything after it. ``{"kind":"answer","retrieval_status":...`` arrived as
``":"answer","retrieval_status":...``.

Downstream that reads as "the model did not answer in JSON", because a parser
asked to find a JSON object finds no opening brace at all. The turn was fine;
the transport ate the first byte.

The repair does not guess. A dropped head is always a prefix of the body, so
re-attaching the missing characters restores exactly the text the model sent —
and a candidate is accepted only when it parses to a JSON object AND satisfies
the caller's own contract check. A wrong reconstruction therefore cannot be
mistaken for a right one: it fails the same schema check the intact body passes.

The contract is supplied by the caller, because this repository has several turn
contracts and they do not share an opening. The customer-QA turn opens
``{"kind":"answer"``, the diagnosis turn opens ``{"kind":"diagnosis"``, and the
zero-order answer has no ``kind`` at all — it opens ``{"text"``. A repair that
knew only the QA opening would leave the other three schemas unrecoverable while
appearing to fix the problem.

Any single dropped head is a prefix *of* an opening, so the cut can land inside
a key name (a real turn lost exactly ``{"kind``, ending mid-``"kind"``). The
search is therefore over every prefix length, not over token boundaries: a
token-aligned list cannot rebuild a mid-token cut, which is how a first version
of this module repaired nothing at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

#: A model that wraps its JSON in a markdown fence, which some providers emit
#: even when the contract asked for bare JSON.
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _all_prefixes(openings: tuple[str, ...]) -> tuple[str, ...]:
    """Every prefix of every opening, longest first.

    Longest first, so the fuller reconstruction is preferred when several parse.
    """
    seen: dict[str, None] = {}
    for opening in openings:
        for length in range(1, len(opening) + 1):
            seen[opening[:length]] = None
    return tuple(sorted(seen, key=len, reverse=True))


#: The openings this repository's turn contracts actually use, and the check
#: that decides whether a reconstruction is that contract's turn. Each entry is
#: ``(openings, is_valid)``; a caller selects the ones its schema can produce.
QA_TURN_OPENINGS: tuple[str, ...] = (
    '{"kind":"answer",',
    '{"kind": "answer",',
    '{"kind":"tool_requests",',
    '{"kind": "tool_requests",',
)

DIAGNOSIS_TURN_OPENINGS: tuple[str, ...] = (
    '{"kind":"diagnosis",',
    '{"kind": "diagnosis",',
    '{"kind":"tool_requests",',
    '{"kind": "tool_requests",',
)

CLASSIFIER_TURN_OPENINGS: tuple[str, ...] = (
    '{"intent":',
    '{"intent": ',
    "{",
)

ZERO_ORDER_OPENINGS: tuple[str, ...] = (
    '{"text":',
    '{"text": ',
    "{",
)


def _has_str(parsed: Any, key: str) -> bool:
    return isinstance(parsed, dict) and isinstance(parsed.get(key), str)


def _looks_like_qa_turn(parsed: Any) -> bool:
    """The customer-QA contract, in both shapes real providers emit.

    The runtime accepts the contract shape (``answer.blocks``) and the flat shape
    (``blocks`` at top level), so the repair must accept both or it leaves half
    the real traffic unrepaired.
    """
    if not isinstance(parsed, dict):
        return False
    kind = parsed.get("kind")
    if kind == "tool_requests":
        return isinstance(parsed.get("tool_requests"), list)
    if kind == "answer":
        nested = parsed.get("answer")
        if isinstance(nested, dict) and isinstance(nested.get("blocks"), list):
            return True
        return isinstance(parsed.get("blocks"), list)
    return False


def _looks_like_diagnosis_turn(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    kind = parsed.get("kind")
    if kind == "tool_requests":
        return isinstance(parsed.get("tool_requests"), list)
    if kind == "diagnosis":
        return "diagnosis" in parsed or "summary" in parsed or "report" in parsed
    return False


def _looks_like_classifier_turn(parsed: Any) -> bool:
    """The classifier always answers with these four keys."""
    if not isinstance(parsed, dict):
        return False
    return _has_str(parsed, "intent") and _has_str(parsed, "confidence")


def _looks_like_zero_order_turn(parsed: Any) -> bool:
    """``{"text": str, "reminder": bool}`` — the staged-reference answer."""
    if not isinstance(parsed, dict) or not _has_str(parsed, "text"):
        return False
    return isinstance(parsed.get("reminder"), bool)


def _bodies(text: str) -> list[str]:
    """The candidate bodies of a turn, in the order a parser should try them."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    candidates: list[str] = [stripped]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE.finditer(stripped))
    return [candidate for candidate in candidates if candidate]


def repair_truncated_turn_head(
    text: str,
    *,
    openings: tuple[str, ...],
    is_valid: Callable[[Any], bool],
) -> dict[str, Any] | None:
    """Parse a turn whose opening characters were lost, or ``None``.

    ``openings`` are the string prefixes the caller's contract can start with;
    ``is_valid`` is the caller's own contract check, which is what keeps a wrong
    reconstruction from being mistaken for a right one.
    """
    prefixes = _all_prefixes(openings)
    for body in _bodies(text):
        if not body or body.startswith("{"):
            # An intact opening is not this function's business; the caller
            # parses first and only falls back here.
            continue
        for prefix in prefixes:
            try:
                parsed = json.loads(prefix + body)
            except (ValueError, TypeError):
                continue
            if is_valid(parsed):
                return parsed
    return None


def parse_turn(
    text: str,
    *,
    openings: tuple[str, ...] = QA_TURN_OPENINGS,
    is_valid: Callable[[Any], bool] = _looks_like_qa_turn,
) -> dict[str, Any] | None:
    """Parse a streamed model turn into a JSON object, or ``None``.

    Tolerant in the ways a real provider makes necessary, tried in order: a
    fenced block, an outermost ``{...}`` span, and finally a body whose head was
    dropped by the transport (see the module docstring).

    This is the one parser for every JSON turn in the runtime, which is why the
    contract is a parameter: the QA path, the light classifier, the zero-order
    answer and the diagnosis turn share the transport defect but not the schema.
    """
    for body in _bodies(text):
        start, end = body.find("{"), body.rfind("}")
        candidates = [body]
        if start != -1 and end > start:
            candidates.append(body[start : end + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return repair_truncated_turn_head(text, openings=openings, is_valid=is_valid)
