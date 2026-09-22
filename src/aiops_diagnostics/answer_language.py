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

The guard's INPUT is a contract too (#363, #366). It takes an
:class:`AnswerSurface` — the blocks a surface is about to deliver, each carrying
its own `kind`, its own `title`, and the media descriptor mounted on it — or a
plain string for the surfaces that finalise on text alone. Nothing else: the
guard used to accept `Any` and pick a branch by shape, and the one production
shape that needed the exemption — a bare list of block dicts — fell through to a
shape-blind flatten that knew neither a block's kind nor its field names, so the
same blocks reached opposite conclusions depending on the Python shape the caller
chose. That branch is gone, and a payload that is neither an `AnswerSurface` nor
a string is a ``TypeError`` rather than a second path: a shape mistake cannot
quietly change which rule judges the answer.
`AnswerSurface.from_public_blocks` builds one from the public ``blocks[]`` a
surface delivers, so a new surface reaches the exemption by construction rather
than by remembering to. A surface that finalises on something other than public
blocks — the diagnosis document, which has none — builds the same type from what
it does have.
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


@dataclass(frozen=True, slots=True)
class AnswerBlock:
    """One answer block as the language guard judges it.

    Every string the public block puts in front of the user is a field here:
    its `text`, its `title` — the block's own and the mounted descriptor's — and
    the resource and chunk ids it cites, with the `kind` that says which of them
    name a knowledge-base resource. That is the whole point of the type: a
    block-level title and a `media.title` used to live on two layers, so the
    same Chinese resource name was exempted as a title and never judged at all
    as a descriptor.
    """

    kind: str
    text: str = ""
    title: str = ""
    media_title: str = ""
    identifiers: tuple[str, ...] = ()
    #: The resource names this run actually retrieved.
    #:
    #: ``None`` means the caller supplied no provenance at all; a (possibly
    #: empty) set means it did. The two must not collapse: an empty set is a
    #: real answer — retrieval returned items, none of which carried a document
    #: name (`normalize_search_response` accepts ``title=None``, and such a chunk
    #: can still have its image signed and cited) — and treating it as "no
    #: provenance" would fall back to the shape-only rule and deliver a
    #: model-authored heading. Only ``None`` may fall back.
    retrieved_titles: frozenset[str] | None = None

    def leaked_chinese(self) -> str:
        """The Chinese this block leaks, honouring the resource-name exemption."""
        leaked: set[str] = set()
        for title in self._judged_titles():
            leaked.update(chinese_leak(title))
        leaked.update(chinese_leak(self.text))
        for identifier in self.identifiers:
            leaked.update(chinese_leak(identifier))
        return "".join(sorted(leaked))

    def _judged_titles(self) -> list[str]:
        """The titles that are prose rather than a resource this run retrieved.

        The exemption is by SOURCE TRUTH, not by shape. A title is display text
        the user reads, and the only thing that makes it a resource name rather
        than prose is that the library actually returned it this run — so a
        title is exempt when it matches one of :attr:`retrieved_titles`.

        Shape alone cannot decide this. `操作步骤` is short and punctuation-free,
        exactly like a filename, so a shape rule accepts a Chinese heading the
        model invented and delivers it to a reader who asked for English. ADR-0007
        named that gap: closing it needs the resource's provenance, not a
        cleverer pattern.

        The mounted descriptor's title is exempt on the same test. A reference
        block carries no descriptor at all (`to_public_dict` mounts `media` for
        image/video only), which is why the exemption cannot key on "matches the
        descriptor" — that would withdraw it from every reference card.

        The ids stay outside all of this. An id is a handle the library issued,
        and a Chinese one reaches the reader as Chinese all the same.
        """
        titles = [self.title, self.media_title]
        if self.retrieved_titles is None:
            # No provenance was supplied: keep the pre-#376 rule, so a surface
            # that finalises without retrieval data is judged as it always was.
            # The risk is a surface that HAS the data and forgets to pass it,
            # restoring the hole #376 closes — a source-level guard asserts the
            # production caller always supplies it (test_answer_caller_shapes.py).
            #
            # Only `None` falls back. An EMPTY set is provenance too: it says
            # retrieval returned nothing usable, so nothing is exempt and a
            # model-authored heading is judged like any other prose.
            if self.kind not in _ASSET_TITLE_KINDS:
                return titles
            return [title for title in titles if not looks_like_asset_name(title)]
        # Provenance supplied. Exempt only when BOTH hold, because either rule
        # alone is wrong in one direction:
        #
        #   - shape alone exempts `操作步骤`, a heading the model invented, which
        #     is as short and punctuation-free as a filename (#376);
        #   - provenance alone exempts a resource named with a sentence, which
        #     ADR-0007 judges as prose by value SHAPE.
        #
        # The conjunction is what a resource name actually is: it looks like one
        # AND the library returned it this run.
        if self.kind not in _ASSET_TITLE_KINDS:
            return [title for title in titles if title not in self.retrieved_titles]
        return [
            title for title in titles if not (looks_like_asset_name(title) and title in self.retrieved_titles)
        ]


@dataclass(frozen=True, slots=True)
class AnswerSurface:
    """The answer a surface is about to deliver: its blocks, in order.

    Frozen on purpose — the guard reads a settled payload and must not be able
    to change what gets delivered. It holds state of no kind beyond its blocks.
    """

    blocks: tuple[AnswerBlock, ...] = ()

    @classmethod
    def from_public_blocks(
        cls,
        blocks: Iterable[Mapping[str, Any]],
        *,
        retrieved_titles: Iterable[str] | None = None,
    ) -> AnswerSurface:
        """Build a surface from the public ``blocks[]`` dicts a surface delivers.

        The single construction path: every finalisation point runs the blocks
        it is about to publish through here before the guard judges them, so the
        resource-name exemption cannot be sidestepped by picking a different
        Python shape. Non-mapping entries are skipped, as are keys the guard
        does not judge — see :func:`_media_title_of` and :func:`_identifiers_of`.
        """
        exempt: frozenset[str] | None = (
            None
            if retrieved_titles is None
            else frozenset(_text_of(name) for name in retrieved_titles if _text_of(name))
        )
        surface_blocks = [
            AnswerBlock(
                kind=str(block.get("kind") or ""),
                text=_text_of(block.get("text")),
                title=_text_of(block.get("title")),
                media_title=_media_title_of(block),
                identifiers=_identifiers_of(block),
                retrieved_titles=exempt,
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


def _media_title_of(block: Mapping[str, Any]) -> str:
    """The mounted descriptor's title — the knowledge-base resource's own name.

    Only the title is read. The rest of the public descriptor (`url`, `kind`,
    `mime_type` and its ids) is issued by the harness at runtime rather than
    copied out of the library, so there is no knowledge-base text left in it for
    the guard to judge.
    """
    descriptor = block.get("media")
    return _text_of(descriptor.get("title")) if isinstance(descriptor, Mapping) else ""


def _identifiers_of(block: Mapping[str, Any]) -> tuple[str, ...]:
    """The ids the block cites: the media resource, the chunk or the document.

    Judged, not exempted. They are part of the payload the user receives, so the
    guard's promise covers them — and the pre-contract production payload did
    judge them, as leaves of the shape-blind flatten. Dropping them here would
    be a silent narrowing of what "no Chinese in the delivered answer" means.
    """
    ids = (block.get("resource_id"), block.get("reference_id"))
    return tuple(_text_of(value) for value in ids if value)


def answer_chinese_leak(payload: AnswerSurface | str, language: str) -> str:
    """Return the Chinese characters ``payload`` leaks for ``language``.

    ``payload`` is one of two things, and which one is declared by the caller
    rather than guessed at: an :class:`AnswerSurface` — from the public
    ``blocks[]`` when the surface delivers blocks, and built directly by the
    surface when it delivers something else — or a plain string for the surfaces
    that finalise on text alone (the zero-order answer and the casual answer).
    Any other value raises ``TypeError``: the guard used to dispatch on shape,
    and the shape a caller happened to pick decided whether the resource-name
    exemption applied at all. A shape mistake must not be able to change which
    rule judges the answer.

    Empty when the answer is clean, when the language is Chinese, when the
    language is not supported, or when the only Chinese present sits inside a
    title that is the resource's name.

    ``language`` values outside the supported set are left alone: the surfaces
    already fall back to the default language upstream, and rejecting here
    would turn a language-resolution detail into a delivery failure.
    """
    if not isinstance(payload, (AnswerSurface, str)):
        raise TypeError(
            "answer payload must be an AnswerSurface or a str, got "
            f"{type(payload).__name__}; build the payload's blocks with "
            "AnswerSurface.from_public_blocks"
        )
    if language not in NON_CHINESE_LANGUAGES:
        return ""
    if isinstance(payload, str):
        return chinese_leak(payload)
    return payload.leaked_chinese()


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
    "answer_chinese_leak",
    "looks_like_asset_name",
    "record_answer_language_fallback",
]


# The default language is re-exported for callers that need to compare against
# it without importing i18n directly.
DEFAULT = DEFAULT_LANGUAGE
