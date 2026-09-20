"""Accept-Language resolution for the assistant/FAQ surface (L1/#201).

Pure header parsing: no framework, storage, or auth dependency. The resolved
language is presentation metadata only — it must never widen permissions,
change routing semantics, or reach the incident manifest (#200 decision).
"""

from __future__ import annotations

import re

# Must stay aligned with the i18n catalog shipped in faq_catalog.json (#200).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en", "de", "fr", "es", "pt")
DEFAULT_LANGUAGE = "zh"

# CJK ideographs and CJK punctuation — what a stored Chinese value looks like
# when it is copied into an answer meant for a reader of another language.
#
# Covers the CJK punctuation blocks (U+3000-303F) and the fullwidth forms
# (U+FF00-FFEF). The fullwidth block also holds fullwidth LATIN letters, so a
# non-Chinese answer using them as a typographic choice would be flagged — an
# accepted, deliberate edge: fullwidth punctuation in an English answer means
# the model is writing through a Chinese input method, and Chinese is what
# follows. The cost of the false positive is one retry; the cost of the false
# negative was a customer reading a language they could not.
CJK_TEXT = re.compile(r"[㐀-䶿一-鿿　-〿＀-￯]")

# Languages whose answers must not contain Chinese. Derived, never listed, so a
# new supported language is covered the moment it is added.
NON_CHINESE_LANGUAGES = frozenset(SUPPORTED_LANGUAGES) - {DEFAULT_LANGUAGE}


# A Chinese name glossed beside its Latin form — `TrendPower (趋势智能)`. The
# Chinese IS the proper noun, so it is an identifier, and dropping it would make
# the answer unciteable to a reader who knows the company by that name.
#
# Deliberately narrow. It matches ONLY a parenthesised Chinese run that directly
# follows Latin text, because that is the shape a gloss has. A bare Chinese name
# (`特来电`) is NOT exempt: telling a cited proper noun apart from a sentence
# needs semantics a pattern does not have, and guessing would reopen the hole
# this guard exists to close. The cost of the narrow rule is a rare withhold
# that an operator sees in the log; the cost of a broad one is Chinese read by a
# customer.
_NAME_GLOSS = re.compile(r"(?<=[A-Za-z0-9])\s*[\(（]\s*[㐀-䶿一-鿿]{1,12}\s*[\)）]")


def _without_name_glosses(text: str) -> str:
    """Remove `(中文名)` glosses that follow a Latin token.

    A gloss is an identifier written twice, not prose: `TrendPower (趋势智能)`
    names the company in both scripts the reader might know it by. Removing it
    before the CJK scan is what keeps the guard from treating a proper noun as
    a leak. See ``_NAME_GLOSS`` for why the rule stays narrow.
    """
    return _NAME_GLOSS.sub("", text)


def chinese_leak(text: str) -> str:
    """Return the distinct Chinese characters in ``text`` (empty when clean).

    Answers for a non-Chinese language may not contain Chinese at all — source
    data is Chinese, so a copied-out value reads as garbage to the reader. This
    is the single shared judgement behind every answer surface's guard; it lives
    here, beside the language tables, so a surface cannot quietly reimplement it.

    Proper nouns glossed in parentheses are removed before judgement; everything
    else is judged as-is.

    Scope: this proves the answer did not LEAK Chinese. It says nothing about
    whether the translation is correct — that is not decidable by pattern.
    """
    return "".join(sorted(set(CJK_TEXT.findall(_without_name_glosses(text)))))


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
#
# Every table below covers ALL of SUPPORTED_LANGUAGES. A missing language is not
# harmless: resolve_language accepts de/fr/es/pt, so a two-language table makes
# the service claim a language it then answers in Chinese — the exact mismatch
# this module exists to prevent (41 live, 2026-09-18).
PROMO_EMPTY_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "case_exploration": "当前没有可用的客户案例，未检索到匹配的宣传资料。",
        "solution_discovery": "当前没有可用的行业方案，未检索到匹配的宣传资料。",
    },
    "en": {
        "case_exploration": "No matching customer case is available in the promotional library.",
        "solution_discovery": "No matching industry solution is available in the promotional library.",
    },
    "de": {
        "case_exploration": "In der Werbematerial-Bibliothek ist kein passender Kundenfall verfügbar.",
        "solution_discovery": "In der Werbematerial-Bibliothek ist keine passende Branchenlösung verfügbar.",
    },
    "fr": {
        "case_exploration": "Aucun cas client correspondant n'est disponible dans la bibliothèque.",
        "solution_discovery": (
            "Aucune solution sectorielle correspondante n'est disponible dans la bibliothèque."
        ),
    },
    "es": {
        "case_exploration": "No hay ningún caso de cliente coincidente en la biblioteca promocional.",
        "solution_discovery": "No hay ninguna solución sectorial coincidente en la biblioteca promocional.",
    },
    "pt": {
        "case_exploration": "Não há nenhum caso de cliente correspondente na biblioteca promocional.",
        "solution_discovery": "Não há nenhuma solução setorial correspondente na biblioteca promocional.",
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
    "de": {
        "case_exploration": "Kundenfälle sind vorübergehend nicht verfügbar. Bitte später erneut versuchen.",
        "solution_discovery": (
            "Branchenlösungen sind vorübergehend nicht verfügbar. Bitte später erneut versuchen."
        ),
    },
    "fr": {
        "case_exploration": "Les cas clients sont momentanément indisponibles. Veuillez réessayer plus tard.",
        "solution_discovery": (
            "Les solutions sectorielles sont momentanément indisponibles. Veuillez réessayer plus tard."
        ),
    },
    "es": {
        "case_exploration": (
            "Los casos de cliente no están disponibles temporalmente. Inténtelo de nuevo más tarde."
        ),
        "solution_discovery": (
            "Las soluciones sectoriales no están disponibles temporalmente. Inténtelo de nuevo más tarde."
        ),
    },
    "pt": {
        "case_exploration": (
            "Os casos de cliente estão temporariamente indisponíveis. Tente novamente mais tarde."
        ),
        "solution_discovery": (
            "As soluções setoriais estão temporariamente indisponíveis. Tente novamente mais tarde."
        ),
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
    "de": {
        "order_no": "Bitte wählen Sie zuerst den zu prüfenden Auftrag aus, damit ich fortfahren kann.",
        "context": "Bitte geben Sie die Auftrags- oder Gerätedaten an, damit ich fortfahren kann.",
        "wrong_entry": "Bitte öffnen Sie die entsprechende Seite über die Schaltfläche auf der Seite.",
    },
    "fr": {
        "order_no": "Veuillez d'abord sélectionner la commande à vérifier pour que je puisse continuer.",
        "context": (
            "Veuillez fournir les informations de commande ou d'appareil pour que je puisse continuer."
        ),
        "wrong_entry": "Veuillez utiliser le bouton de la page pour ouvrir l'écran correspondant.",
    },
    "es": {
        "order_no": "Seleccione primero el pedido que desea revisar para que yo pueda continuar.",
        "context": "Facilite los datos del pedido o del dispositivo para que yo pueda continuar.",
        "wrong_entry": "Utilice el botón de la página para abrir la pantalla correspondiente.",
    },
    "pt": {
        "order_no": "Selecione primeiro o pedido que deseja verificar para que eu possa continuar.",
        "context": "Forneça os dados do pedido ou do dispositivo para que eu possa continuar.",
        "wrong_entry": "Utilize o botão da página para abrir o ecrã correspondente.",
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
