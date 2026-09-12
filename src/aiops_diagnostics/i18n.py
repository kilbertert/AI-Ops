"""Accept-Language resolution for the assistant/FAQ surface (L1/#201).

Pure header parsing: no framework, storage, or auth dependency. The resolved
language is presentation metadata only — it must never widen permissions,
change routing semantics, or reach the incident manifest (#200 decision).
"""

from __future__ import annotations

# Must stay aligned with the i18n catalog shipped in faq_catalog.json (#200).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en", "de", "fr", "es", "pt")
DEFAULT_LANGUAGE = "zh"


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
