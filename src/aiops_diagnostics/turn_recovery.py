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
the turn contract (a ``kind`` the schema allows, and for an answer the blocks
the schema requires). A wrong reconstruction therefore cannot be mistaken for a
right one: it fails the same schema check the intact body passes.

Only prefixes the schema can enumerate are tried. ``{"kind":"answer"`` and
``{"kind":"tool_requests"`` are the only two openings the contract permits, and
whitespace between the tokens is the only variation — so the search space is the
cartesian product of the two openings and a few spacings, not an open-ended
string match. Anything further from the contract than that is not repaired here;
it is retried or salvaged by the caller.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The two bodies a turn can open with, exactly as the schema orders them.
#: Spacing variants cover a model that pretty-prints the same tokens.
_TURN_OPENINGS: tuple[str, ...] = (
    '{"kind":"answer",',
    '{"kind": "answer",',
    '{"kind":"tool_requests",',
    '{"kind": "tool_requests",',
)

#: Characters the head loss has been observed to reach. A reconstruction longer
#: than this is not attempted: it would mean the model opened with something the
#: contract does not permit, which is not a transport defect.
_MAX_PREFIX = 32


def _prefix_candidates() -> tuple[str, ...]:
    """Every prefix of every opening, longest first.

    Every *prefix*, not every token boundary: the transport cuts wherever the
    delta boundary happens to fall, and it is not aligned to JSON tokens. A real
    turn lost exactly ``{"kind`` — six characters, ending in the middle of the
    ``"kind"`` key — leaving a body that starts ``":"answer",...``. A candidate
    list built from whole tokens can never rebuild that, which is how a first
    version of this module repaired nothing at all.

    Longest first, so the fuller reconstruction is preferred when several parse.
    """
    seen: dict[str, None] = {}
    for opening in _TURN_OPENINGS:
        for length in range(1, min(len(opening), _MAX_PREFIX) + 1):
            seen[opening[:length]] = None
    return tuple(sorted(seen, key=len, reverse=True))


_PREFIXES: tuple[str, ...] = _prefix_candidates()

#: A model that wraps its JSON in a markdown fence, which some providers emit
#: even when the contract asked for bare JSON.
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _looks_like_a_turn(parsed: Any) -> bool:
    """True when a reconstruction satisfies the turn contract, not just JSON.

    This is what keeps the repair honest. A repair that merely produced valid
    JSON would happily re-attach the wrong opening and hand back a turn the
    model never wrote; requiring the contract's own discriminator means a wrong
    guess fails here exactly as it would fail the real parser.

    Both answer shapes real providers emit are accepted, because the runtime
    accepts both: the contract shape (``answer.blocks``) and the flat shape
    (``blocks`` at turn top level). A repair that rejected the flat shape would
    leave half the real traffic unrepaired.
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


def repair_truncated_turn_head(text: str) -> dict[str, Any] | None:
    """Parse a turn whose opening characters were lost, or ``None``.

    Returns the reconstructed object, so the caller keeps one parse path: the
    repaired body is treated as the model's own answer, because that is what it
    is.
    """
    for body in _bodies(text):
        if not body or body.startswith("{"):
            # An intact opening is not this function's business; the caller
            # parses first and only falls back here.
            continue
        for prefix in _PREFIXES:
            try:
                parsed = json.loads(prefix + body)
            except (ValueError, TypeError):
                continue
            if _looks_like_a_turn(parsed):
                return parsed
    return None


def _bodies(text: str) -> list[str]:
    """The candidate bodies of a turn, in the order a parser should try them."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    candidates: list[str] = [stripped]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE.finditer(stripped))
    return [candidate for candidate in candidates if candidate]


def parse_turn(text: str) -> dict[str, Any] | None:
    """Parse a streamed model turn into a JSON object, or ``None``.

    Tolerant in the two ways a real provider makes necessary, tried in order:
    a fenced block, an outermost ``{...}`` span, and finally a body whose head
    was dropped by the transport (see the module docstring).

    This is the one parser for every JSON turn in the runtime. Two copies of
    this logic is how the QA path and the lightweight classifier came to
    disagree about what counts as an invalid turn — the classifier had no
    head-loss fallback at all, so the same transport defect reached it as a
    routing failure instead of an answer.
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
    return repair_truncated_turn_head(text)
