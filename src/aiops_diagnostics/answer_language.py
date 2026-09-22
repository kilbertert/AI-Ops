"""Output-side language guard for the answer surfaces (#293).

Answers reach users through several paths — the promotional card, customer QA,
the zero-order answer, the lightweight casual answer, and the standard
diagnosis. Before this module, only the diagnosis path checked the language it
actually produced, so a Chinese leak was found and fixed one surface at a time
while its siblings stayed broken.

The judgement itself lives in :mod:`aiops_diagnostics.i18n` beside the language
tables; this module is the shared *application* of it, so a surface opts in by
calling one function rather than reimplementing the rule.

What the guard proves and what it does not: it proves the answer did not LEAK
Chinese. It cannot prove the translation is correct — that is a semantic
judgement, undecidable by pattern. Verification records must not conflate the
two.

Titles are exempt, by shape. A `title` on a media or reference block is the
knowledge-base resource's name — `新加坡无人电动巴士.mp4`, or an extensionless
document name like `新加坡案例介绍`. It is an identifier: translating it would
leave the answer citing material the reader cannot match to the library.

The exemption is scoped to the two block kinds whose `title` the contract
defines as a resource name (image/video carry the media name, reference carries
the source document name). It is NOT a blanket pass on the `title` key: the
value must be name-shaped — short, no sentence punctuation, no prose. Text
blocks carry no title at all, so prose cannot reach the exemption.

The guard's INPUT is a contract too (#363). It takes an :class:`AnswerSurface`
— the blocks a surface is about to deliver, each carrying its own `kind`, its
own `title`, and the media descriptor mounted on it — or a plain string for the
surfaces that finalise on text alone. What it does not take is "any value": the
guard used to accept `Any` and pick a branch by shape, and the one production
shape that needed the exemption — a bare list of block dicts — fell through to a
shape-blind flatten that knew neither a block's kind nor its field names. Same
blocks, opposite conclusions, depending on the Python shape the caller chose.
`AnswerSurface.from_public_blocks` is the single construction path, so a new
surface reaches the exemption by construction rather than by remembering to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    NON_CHINESE_LANGUAGES,
    chinese_leak,
)

logger = logging.getLogger(__name__)

# Asset filename extensions the knowledge base actually stores. A title ending
# in one of these is a resource name, not prose.
ASSET_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".csv",
    ".md",
)

# Block kinds whose `title` the public contract defines as a resource name:
# image/video carry the media resource's name, reference the source document's.
# A `text` block has no title, so prose is never in scope for the exemption.
_ASSET_TITLE_KINDS = frozenset({"image", "video", "reference"})


# Sentence punctuation in any of the supported languages. A resource name does
# not contain it; prose does. Covers the fullwidth and ideographic marks too,
# because the source data IS Chinese — leaving them out let a Chinese sentence
# ending in `.pdf` pass as a filename.
_PROSE_PUNCTUATION = (
    "\n",
    "\r",
    ". ",
    "。",
    "！",
    "？",
    "，",
    "、",
    "；",
    "：",
    "；",
    "：",
    "…",
    "—",
    "!",
    "?",
    ",",
    ";",
    ":",
)

# A resource name is a label, not a paragraph. Chinese names are information
# dense, so the cap is generous on purpose — but a 120-character string is a
# sentence, whatever it ends with.
_MAX_RESOURCE_NAME = 80


def looks_like_asset_name(value: str) -> bool:
    """True when ``value`` reads as a resource name rather than prose.

    Judged on shape alone, and deliberately in the conservative direction: it
    must look like a NAME. Anything carrying sentence punctuation is prose even
    if it ends in a document extension, so a Chinese sentence ending in `.pdf`
    is judged rather than exempted. Extensionless names are accepted — the
    knowledge base stores them, and they are still names.
    """
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_RESOURCE_NAME:
        return False
    if any(mark in candidate for mark in _PROSE_PUNCTUATION):
        return False
    # With a stored extension it is a resource name: the extension IS the
    # signal, and the punctuation check above already excluded prose.
    if candidate.lower().endswith(ASSET_EXTENSIONS):
        return True
    # Without one there is no extension signal to lean on, so require a real
    # name signal instead of guessing by length. Chinese has no spaces, so a
    # whole Chinese clause counts as one "word" — length is not a usable proxy
    # here, and treating it as one exempted paragraphs.
    return _looks_like_label(candidate)


def _looks_like_label(value: str) -> bool:
    """True for a short label that reads as a name rather than a clause.

    Korean/Japanese/Chinese names and Latin identifiers share no single shape,
    so this stays narrow on purpose: short, and either a single token or a
    small number of space-separated tokens. Anything longer is prose and is
    judged by the caller rather than exempted here.
    """
    tokens = value.split()
    if len(tokens) > 4:
        return False
    if len(value) <= 20:
        # A short, punctuation-free string is a label in every supported
        # language: a document name, a site name, a product name.
        return True
    return len(tokens) >= 2


def _flatten(value: Any, key: str | None = None) -> Iterable[tuple[str | None, str]]:
    """Yield ``(parent_key, leaf_string)`` for every string in ``value``.

    ponytail: shape-blind by construction — it cannot know a block's kind, so it
    cannot honour the resource-name exemption. Ceiling and upgrade trigger: the
    bare-list payload shape it exists for is migrated in #364, and #366 deletes
    this path with the rest of the shape dispatch.
    """
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from _flatten(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item, key)


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    """The knowledge-base resource descriptor a media block carries.

    Its title is the resource's own name. The rest of the public descriptor
    (`url`, `kind`, `mime_type` and the ids) is issued by the harness at
    runtime rather than copied out of the library, so there is no knowledge-base
    text left in it for the guard to judge.
    """

    title: str = ""


@dataclass(frozen=True, slots=True)
class AnswerBlock:
    """One answer block as the language guard judges it.

    `kind` decides how the block's text is read, and `title` — the block's own
    and the mounted descriptor's — belongs here, to the block that carries it.
    That is the whole point of the type: a block-level title and a
    `media.title` used to live on two layers, so the same Chinese resource name
    was exempted as a title and never judged at all as a descriptor.
    """

    kind: str
    text: str = ""
    title: str = ""
    media: MediaDescriptor | None = None

    def leaked_chinese(self) -> str:
        """The Chinese this block leaks, honouring the resource-name exemption."""
        leaked: set[str] = set()
        for title in self._judged_titles():
            leaked.update(chinese_leak(title))
        leaked.update(chinese_leak(self.text))
        return "".join(sorted(leaked))

    def _judged_titles(self) -> list[str]:
        """The titles that are prose rather than the resource's name.

        One implementation of the exemption, applied to the block's own title
        and to the descriptor mounted on it alike: a title on an image, video
        or reference block names the knowledge-base resource, so translating it
        would leave the answer citing material the reader cannot match back to
        the library. A title on any other kind is prose and is judged.
        """
        titles = [self.title]
        if self.media is not None:
            titles.append(self.media.title)
        if self.kind not in _ASSET_TITLE_KINDS:
            return titles
        return [title for title in titles if not looks_like_asset_name(title)]


@dataclass(frozen=True, slots=True)
class AnswerSurface:
    """The answer a surface is about to deliver: its blocks, in order.

    Frozen on purpose — the guard reads a settled payload and must not be able
    to change what gets delivered. It holds state of no kind beyond its blocks.
    """

    blocks: tuple[AnswerBlock, ...] = ()

    @classmethod
    def from_public_blocks(cls, blocks: Iterable[Mapping[str, Any]]) -> AnswerSurface:
        """Build a surface from the public ``blocks[]`` dicts a surface delivers.

        The single construction path: every finalisation point runs the blocks
        it is about to publish through here before the guard judges them, so the
        resource-name exemption cannot be sidestepped by picking a different
        Python shape. Non-mapping entries are skipped, as are keys the guard
        does not judge — see :class:`MediaDescriptor`.
        """
        surface_blocks = [
            AnswerBlock(
                kind=str(block.get("kind") or ""),
                text=_text_of(block.get("text")),
                title=_text_of(block.get("title")),
                media=_descriptor_of(block.get("media")),
            )
            for block in blocks
            if isinstance(block, Mapping)
        ]
        return cls(blocks=tuple(surface_blocks))

    def leaked_chinese(self) -> str:
        """The Chinese the whole payload leaks for a non-Chinese language."""
        leaked: set[str] = set()
        for block in self.blocks:
            leaked.update(block.leaked_chinese())
        return "".join(sorted(leaked))


def _text_of(value: Any) -> str:
    """The value when it is text, empty otherwise — only strings are judged."""
    return value if isinstance(value, str) else ""


def _descriptor_of(value: Any) -> MediaDescriptor | None:
    """The mounted media descriptor, when the block really carries one."""
    return MediaDescriptor(title=_text_of(value.get("title"))) if isinstance(value, Mapping) else None


def answer_chinese_leak(payload: AnswerSurface | str, language: str) -> str:
    """Return the Chinese characters ``payload`` leaks for ``language``.

    ``payload`` is one of two things, and which one is declared by the caller
    rather than guessed at: an :class:`AnswerSurface` built through
    ``AnswerSurface.from_public_blocks``, or a plain string for the surfaces
    that finalise on text alone (the zero-order answer and the casual answer).
    Every other shape is a caller bug, not a shape to dispatch on.

    Empty when the answer is clean, when the language is Chinese, when the
    language is not supported, or when the only Chinese present sits inside a
    title that is the resource's name.

    ``language`` values outside the supported set are left alone: the surfaces
    already fall back to the default language upstream, and rejecting here
    would turn a language-resolution detail into a delivery failure.
    """
    if language not in NON_CHINESE_LANGUAGES:
        return ""
    if isinstance(payload, AnswerSurface):
        return payload.leaked_chinese()
    if isinstance(payload, str):
        return chinese_leak(payload)
    # ponytail: the raw dict/list shapes the pre-contract callers still pass.
    # Same judgement — the dict shape goes through `AnswerSurface`, so the
    # exemption stays a single implementation — but a bare list of blocks
    # carries no kinds, so it is judged leaf by leaf. Ceiling and upgrade
    # trigger: #364/#365 migrate the callers and #366 deletes this branch,
    # turning any other value into a TypeError.
    return _leak_in_unmigrated_payload(payload)


def _leak_in_unmigrated_payload(payload: Any) -> str:
    """Judge a payload no surface has migrated to the contract yet.

    The judge itself is unchanged from the pre-contract guard: a
    ``{"blocks": [...]}`` payload is judged as blocks, a single block dict as one
    block, and anything else leaf by leaf.
    """
    if isinstance(payload, dict):
        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            return AnswerSurface.from_public_blocks(blocks).leaked_chinese()
        return AnswerSurface.from_public_blocks([payload]).leaked_chinese()
    leaked: set[str] = set()
    for _parent, text in _flatten(payload):
        found = chinese_leak(text)
        if found:
            leaked.update(found)
    return "".join(sorted(leaked))


def text_block_leak(blocks: Iterable[Any], language: str) -> str:
    """Return the Chinese leak across the ``text`` blocks of an answer.

    Non-text blocks (media, references) carry resource identifiers and titles,
    which the shape exemption already covers; only prose is judged here.
    """
    if language not in NON_CHINESE_LANGUAGES:
        return ""
    leaked: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("kind") != "text":
            continue
        found = chinese_leak(str(block.get("text") or ""))
        if found:
            leaked.update(found)
    return "".join(sorted(leaked))


def record_answer_language_fallback(*, language: str, leaked: str, surface: str) -> None:
    """Record that an answer was withheld because it leaked Chinese.

    Deliberately loud: the whole reason this defect class survived three rounds
    is that the failure was silent — the request returned 200, the language
    echoed correctly, and only the text was wrong. A structured warning means
    the next occurrence is visible to an operator instead of to a customer.

    Never logs the leaked text itself: it is the model's output, but it is also
    verbatim source data, and the log is not the place to copy it.
    """
    logger.warning(
        "answer language fallback: surface=%s language=%s leaked_chars=%d",
        surface,
        language,
        len(leaked),
        extra={
            "event": "answer_language_fallback",
            "surface": surface,
            "language": language,
            "leaked_char_count": len(leaked),
        },
    )


__all__ = [
    "ASSET_EXTENSIONS",
    "AnswerBlock",
    "AnswerSurface",
    "MediaDescriptor",
    "answer_chinese_leak",
    "looks_like_asset_name",
    "record_answer_language_fallback",
    "text_block_leak",
]


# The default language is re-exported for callers that need to compare against
# it without importing i18n directly.
DEFAULT = DEFAULT_LANGUAGE
