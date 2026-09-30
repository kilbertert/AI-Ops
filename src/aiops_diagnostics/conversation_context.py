"""Conversation history as prompt input (#172, #482).

``ConversationStore.context_turns()`` decides *which* turns are in the window
(8 turns / 8k tokens, completed turns only, single oversized turn kept alone).
This module decides *how* that window becomes text the model reads. Both halves
live here rather than in each generation path, because there was a time when
the window existed in the contract, in the store, and in the tests — and in no
prompt at all.

Three rules the shape is built to keep:

* **Deterministic and labelled, never translated.** A turn was written in the
  language its asker used; this turn may be in another one. Turning the history
  into a second model call would add a second dependency and a second chance to
  drift, so lines are *labelled* in the current language instead.
* **Redacted.** The stored answer is model output *and* once user input.
* **Empty means empty.** With no history the block is the empty string, so every
  prompt that interpolates it is byte-identical to what it was before this
  wiring — that equality is the change's safety boundary, and it is asserted.
"""

from __future__ import annotations

from typing import Any

from aiops_diagnostics.conversation_store import CONTEXT_MAX_TOKENS, CONTEXT_MAX_TURNS
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.redaction import redact_text

#: The window's own limits, as the store defines them. A settings object may
#: override them; these are the defaults every caller falls back to, so the
#: numbers still have exactly one definition.
DEFAULT_MAX_TURNS = CONTEXT_MAX_TURNS
DEFAULT_MAX_TOKENS = CONTEXT_MAX_TOKENS

#: Labels for the two speakers, per supported output language (``i18n``). An
#: unknown language falls back to English rather than to Chinese: the label only
#: has to be readable to the model, and a Chinese heading over a translated
#: exchange reads like stored content rather than like a label.
_LABELS: dict[str, tuple[str, str]] = {
    "zh": ("用户", "助手"),
    "en": ("User", "Assistant"),
    "de": ("Benutzer", "Assistent"),
    "fr": ("Utilisateur", "Assistant"),
    "es": ("Usuario", "Asistente"),
    "pt": ("Usuário", "Assistente"),
}

#: Continuation prefix for extra lines inside one speaker's text. Without it a
#: stored answer containing a newline plus "Assistant:" would read to the model
#: as a turn nobody made — the history is data, and this keeps its shape from
#: being forgeable by its content.
_CONTINUATION = "  "

_HEADERS: dict[str, str] = {
    "zh": "以下是本次会话中此前已完成的问答（仅供理解指代，不作为事实依据；本轮仍以实际查询到的数据为准）：",
    "en": "Earlier completed turns in this conversation (context for resolving "
    "references only — not evidence; this turn still relies on what it actually "
    "retrieves):",
    "de": "Frühere abgeschlossene Runden dieses Gesprächs (nur Kontext zum "
    "Auflösen von Verweisen, kein Beleg; diese Runde stützt sich weiterhin auf "
    "das tatsächlich Abgerufene):",
    "fr": "Tours déjà terminés de cette conversation (contexte pour lever les "
    "références uniquement — pas une preuve ; ce tour s'appuie toujours sur ce "
    "qu'il récupère réellement) :",
    "es": "Turnos ya completados de esta conversación (contexto solo para "
    "resolver referencias, no evidencia; este turno sigue basándose en lo que "
    "realmente recupera):",
    "pt": "Turnos já concluídos desta conversa (contexto apenas para resolver "
    "referências, não evidência; este turno continua a basear-se no que "
    "realmente recupera):",
}


def _labels(language: str) -> tuple[str, str]:
    return _LABELS.get(language, _LABELS["en"])


def _header(language: str) -> str:
    return _HEADERS.get(language, _HEADERS["en"])


def _answer_text(turn: dict[str, Any]) -> str:
    """The readable part of a stored turn answer, in either answer shape.

    A qa/promo turn stores ``blocks[]``; a diagnosis turn stores ``summary`` +
    ``root_cause`` (the same two fields the diagnosis metric counts). One helper
    reads both rather than making callers know which line produced the turn.
    """
    answer = turn.get("answer") or {}
    parts: list[str] = []
    for block in answer.get("blocks") or []:
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    if text := str(answer.get("text") or "").strip():
        parts.append(text)
    for field in ("summary", "root_cause"):
        if value := str(answer.get(field) or "").strip():
            parts.append(value)
    return "\n".join(parts)


def render_history(turns: list[dict[str, Any]], language: str = DEFAULT_LANGUAGE) -> str:
    """The history block for these turns, or ``""`` when there are none.

    Oldest-first (``context_turns`` already returns that order) so the model
    reads the conversation in the order it happened.
    """
    if not turns:
        return ""
    asker, answerer = _labels(language)
    lines: list[str] = []
    for turn in turns:
        question = redact_text(str(turn.get("question") or "").strip())
        answer = redact_text(_answer_text(turn))
        if not question and not answer:
            continue
        if question:
            lines.append(f"{asker}: " + question.replace("\n", "\n" + _CONTINUATION))
        if answer:
            lines.append(f"{answerer}: " + answer.replace("\n", "\n" + _CONTINUATION))
    if not lines:
        return ""
    return _header(language) + "\n" + "\n".join(lines) + "\n\n"


def build_history(
    store: Any,
    conversation_id: str | None,
    scope_fingerprint: str | None,
    language: str = DEFAULT_LANGUAGE,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Read the window and render it, or ``""`` when there is none to read.

    The token budget bounds the window the same way the store defines it: the
    newest turn is kept even if it alone exceeds the budget. A stricter cap
    would silently return nothing for a conversation whose last turn was long,
    which is the failure this window exists to avoid.

    Every failure returns ``""``: an unreadable window must not make a question
    unanswerable. The caller distinguishes the two by logging the exception —
    this function's contract is only "history or no history".

    The bounds are checked here rather than left to ``context_turns()``, whose
    range check is an exception: a mistyped knob must read as "no history" (and
    be visible in the caller's warning), not as a question that cannot be asked.
    """
    if not conversation_id or not scope_fingerprint:
        return ""
    if not 1 <= max_turns <= 50 or not 100 <= max_tokens <= 100000:
        raise ValueError("conversation context limits are out of range")
    turns = store.context_turns(
        conversation_id,
        scope_fingerprint,
        max_turns=max_turns,
        max_tokens=max_tokens,
    )
    return render_history(turns, language)
