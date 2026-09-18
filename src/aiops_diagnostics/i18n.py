"""Accept-Language resolution for the assistant/FAQ surface (L1/#201).

Pure header parsing: no framework, storage, or auth dependency. The resolved
language is presentation metadata only — it must never widen permissions,
change routing semantics, or reach the incident manifest (#200 decision).
"""

from __future__ import annotations

# Must stay aligned with the i18n catalog shipped in faq_catalog.json (#200).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en", "de", "fr", "es", "pt")
DEFAULT_LANGUAGE = "zh"

# Human-readable names used inside model prompts (qa/diagnosis, #204).
LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Simplified Chinese",
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
}

# Presentation copy for the qa retrieval fallback per language (#204).
# zh is authoritative: a missing language falls back to the zh strings.
QA_FALLBACK_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "not_found": "知识库中没有找到与当前问题直接相关的资料，暂时无法提供有依据的回答。",
        "unavailable": "当前知识库暂时不可用，本次回答无法基于知识库确认，请稍后重试。",
        "limited": "本次检索未能完成，请稍后重试或换个问法。",
    },
    "en": {
        "not_found": (
            "No directly relevant material was found in the knowledge base; "
            "no evidence-backed answer is available."
        ),
        "unavailable": (
            "The knowledge base is temporarily unavailable and the answer "
            "could not be verified. Please retry later."
        ),
        "limited": "This lookup could not be completed. Please try again or rephrase your question.",
    },
    "de": {
        "not_found": (
            "In der Wissensbasis fand sich kein passendes Material; "
            "eine belegte Antwort ist derzeit nicht möglich."
        ),
        "unavailable": (
            "Die Wissensbasis ist vorübergehend nicht erreichbar; "
            "die Antwort blieb unverifiziert. Bitte später erneut versuchen."
        ),
        "limited": "Diese Suche konnte nicht abgeschlossen werden. Bitte später erneut versuchen.",
    },
    "fr": {
        "not_found": (
            "Aucun document pertinent n'a été trouvé dans la base de connaissances ; "
            "pas de réponse étayée pour l'instant."
        ),
        "unavailable": (
            "La base de connaissances est momentanément indisponible ; "
            "la réponse n'a pu y être vérifiée. Réessayez plus tard."
        ),
        "limited": "Cette recherche n'a pas abouti. Réessayez plus tard ou reformulez la question.",
    },
    "es": {
        "not_found": (
            "No se halló material pertinente en la base de conocimiento; "
            "no hay respuesta con respaldo por ahora."
        ),
        "unavailable": (
            "La base de conocimiento no está disponible y la respuesta "
            "no pudo verificarse. Inténtelo más tarde."
        ),
        "limited": "No se pudo completar la búsqueda. Inténtelo de nuevo o formule la pregunta otra vez.",
    },
    "pt": {
        "not_found": (
            "Nada de pertinente foi encontrado na base de conhecimento; "
            "não há resposta fundamentada neste momento."
        ),
        "unavailable": (
            "A base de conhecimento está indisponível e a resposta "
            "não pôde ser verificada. Tente novamente mais tarde."
        ),
        "limited": "Não foi possível concluir a busca. Tente novamente ou reformule a sua pergunta.",
    },
}

# Honest empty promotional card when the pinned agent/KB has no match (#231).
PROMO_EMPTY_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "case_exploration": "当前没有可用的客户案例，未检索到匹配的宣传资料。",
        "solution_discovery": "当前没有可用的行业方案，未检索到匹配的宣传资料。",
    },
    "en": {
        "case_exploration": "No matching customer case is available in the promotional library.",
        "solution_discovery": "No matching industry solution is available in the promotional library.",
    },
}

# The promotional surface could not be produced. Deliberately NOT the messages
# above: those assert "the library holds nothing matching this", which is a
# claim about content. When nothing was searched — no resolvable target, no
# search capability, a dependency failure, or an unreachable model — we know
# nothing about the content, so claiming an empty library would be a false
# statement to the user (and a misleading one to whoever debugs it later).
#
# Wording stays subsystem-neutral on purpose: the same copy covers a search
# outage and a model outage, and naming "检索" for a model failure would be a
# fresh inaccuracy of exactly the kind this table exists to prevent.
PROMO_UNAVAILABLE_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "case_exploration": "客户案例服务暂时不可用，请稍后重试。",
        "solution_discovery": "行业方案服务暂时不可用，请稍后重试。",
    },
    "en": {
        "case_exploration": "Customer cases are temporarily unavailable. Please try again later.",
        "solution_discovery": "Industry solutions are temporarily unavailable. Please try again later.",
    },
}


# Sync clarification replies. These are USER-VISIBLE and rendered directly by
# the client (frontend brief D.3: "渲染 message"), so they must follow
# Accept-Language like every other user-facing string. They were hardcoded in
# Chinese while the response still echoed `language: en` — the one user-visible
# surface that silently ignored the request language (41 live, 2026-09-18).
#
# Keys are the missing context the reply asks for; the jump-action guard uses
# "wrong_entry" because nothing is missing there — the client used the wrong
# surface.
CLARIFICATION_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "order_no": "请先选择需要检测的订单后，我才能继续处理。",
        "context": "请补充订单或设备等必要信息后，我才能继续处理。",
        "wrong_entry": "请点击页面上的快捷按钮进入对应页面。",
    },
    "en": {
        "order_no": "Please select the order you want checked before I can continue.",
        "context": "Please provide the order or device details before I can continue.",
        "wrong_entry": "Please use the shortcut button on the page to open the relevant screen.",
    },
}

CLARIFICATION_FALLBACK_KEY = "context"


def clarification_message(language: str, key: str) -> str:
    """Localized clarification text, falling back to zh then to a safe default.

    A missing key must never yield an empty message: the client renders this
    string verbatim, so an empty one would leave the user with a blank reply.
    """
    pack = CLARIFICATION_MESSAGES.get(language) or CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE]
    resolved = pack.get(key) or CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE].get(key)
    if resolved:
        return resolved
    return CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE][CLARIFICATION_FALLBACK_KEY]


def language_name(language: str) -> str:
    """English display name of a supported language for prompt injection."""
    return LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[DEFAULT_LANGUAGE])


def resolve_language(accept_language: str | None) -> str:
    """Resolve one supported language tag from an ``Accept-Language`` header.

    RFC 7231 list semantics: entries split on ``,``; the first param ``q=``
    sets the weight (default 1.0). Region (and script) subtags fold onto the
    base tag (``en-US`` → ``en``, ``zh-Hans-CN`` → ``zh``). The highest-q
    supported base tag wins; equal q keeps the first occurrence. `*`,
    unsupported tags, malformed weights, and q=0 entries are ignored — a
    header with no acceptable entry falls back to :data:`DEFAULT_LANGUAGE`.
    """
    if not accept_language or not accept_language.strip():
        return DEFAULT_LANGUAGE
    best: tuple[float, str] | None = None
    for entry in accept_language.split(","):
        parts = [part.strip() for part in entry.split(";")]
        tag = parts[0].lower()
        if not tag or tag == "*":
            continue
        weight = 1.0
        malformed = False
        for param in parts[1:]:
            if param.startswith("q="):
                try:
                    weight = float(param[2:])
                except ValueError:
                    malformed = True
                    break
                if not 0.0 <= weight <= 1.0:
                    malformed = True
                    break
        if malformed or weight <= 0.0:
            continue
        base = tag.split("-", 1)[0]
        if base not in SUPPORTED_LANGUAGES:
            continue
        if best is None or weight > best[0]:
            best = (weight, base)
    return best[1] if best else DEFAULT_LANGUAGE
