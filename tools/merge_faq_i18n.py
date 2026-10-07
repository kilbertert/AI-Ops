#!/usr/bin/env python3
"""把多语言宽表（xlsx）与五语答案 JSON 合并进 FAQ 目录（#200/#202）。

输入：
  docs/EV_Charging_FAQ_Multilingual_Wide_Table.xlsx  —— 产品提供的 28 条预设问题
      六列宽表（zh 原文 + en/de/fr/es/pt 压缩标题），行序与 consumer.faq.q001..q028 一一对应。
  tools/faq_i18n/answers_{lang}.json                 —— 以中文权威答案为源起草的答案翻译。

输出（原位重写）：
  src/aiops_diagnostics/faq_catalog.json            —— 每条 consumer entry 注入
      "i18n": {"en": {"question": ..., "answer": ...}, ...}，并升级 faq_version。
  src/aiops_diagnostics/faq_recommendations.json    —— 同步补充各语言 title。

仅用标准库解析 xlsx（zipfile + ElementTree），与 tools/generate_faq_catalog.py 的
无依赖风格一致。校验失败（行序漂移、缺答案、空文案、中文原文不一致）直接报错退出，
绝不静默产出残缺目录。zh 是权威原文：zh 不进 i18n，缺失语言在运行时回退 zh。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "aiops_diagnostics" / "faq_catalog.json"
RECOMMENDATIONS = ROOT / "src" / "aiops_diagnostics" / "faq_recommendations.json"
WIDE_TABLE = ROOT / "docs" / "EV_Charging_FAQ_Multilingual_Wide_Table.xlsx"
ANSWER_DIR = ROOT / "tools" / "faq_i18n"

#: One wide table per platform. They are different files with different column
#: counts and different header wording, so a single default silently reads the
#: wrong one for the other platform.
WIDE_TABLES = {
    "consumer": WIDE_TABLE,
    "operator": ROOT / "docs" / "管家端问答国际化宽表.xlsx",
}

#: The operator answer assets are prefixed so a run for one platform cannot pick
#: up the other platform's file by filename collision.
ANSWER_PREFIX = {"consumer": "answers_", "operator": "operator_answers_"}

#: Mirrors ANSWER_PREFIX so a question file cannot collide across platforms the
#: same way an answer file cannot: `questions_vi.json` is consumer's,
#: `operator_questions_vi.json` is the operator entry's.
QUESTIONS_PREFIX = {"consumer": "questions_", "operator": "operator_questions_"}

LANGS = ("en", "de", "fr", "es", "pt")

#: Header label -> language tag, for the columns this tool reads. Matched by
#: HEADER LABEL, not by column letter: the operator wide table is 11 columns, so
#: a letter map silently reads the wrong ones (#534 caught this shape). A
#: language absent here is left alone rather than mis-read.
HEADER_LANGUAGES = {
    "中文-简体": "zh",
    "English": "en",
    "Deutsch": "de",
    "Français": "fr",
    "España": "es",
    "Portugal": "pt",
    "ภาษาไทย": "th",
    "Tiếng Việt": "vi",
    "Монгол": "mn",
    "ភាសាខ្មែរ": "km",
    "中文-繁體": "zh-Hant",
}

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    strings: list[str] = []
    for si in root.findall(f"{{{_NS_MAIN}}}si"):
        strings.append("".join(t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")))
    return strings


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    sheet = workbook.find(f"{{{_NS_MAIN}}}sheets/{{{_NS_MAIN}}}sheet")
    if sheet is None:
        raise ValueError("xlsx workbook has no sheets")
    rid = sheet.attrib[f"{{{_NS_REL_DOC}}}id"]
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall(f"{{{_NS_PKG_REL}}}Relationship"):
        if rel.attrib["Id"] == rid:
            target = rel.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError("xlsx workbook rels are invalid")


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{_NS_MAIN}}}t"))
    value = cell.find(f"{{{_NS_MAIN}}}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        return shared[int(value.text)]
    return value.text


def _column_index(ref: str) -> str:
    return re.match(r"([A-Z]+)", ref).group(1)  # noqa: WPS609 — xlsx cell refs are A1 style


def read_wide_table(path: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Return (rows, column_map) where each row is {language: text}.

    ``column_map`` is built from the header ROW, so a table with more language
    columns than this tool consumes is read correctly and the rest are ignored
    rather than shifted into the wrong language.
    """
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        sheet = ET.fromstring(zf.read(_first_sheet_path(zf)))
    raw: list[dict[str, str]] = []
    for row in sheet.iter(f"{{{_NS_MAIN}}}row"):
        cells: dict[str, str] = {}
        for cell in row.findall(f"{{{_NS_MAIN}}}c"):
            cells[_column_index(cell.attrib.get("r", ""))] = _cell_text(cell, shared)
        raw.append(cells)
    header_row, data_rows = raw[0], raw[1:]
    column_map: dict[str, str] = {}
    for letter, label in header_row.items():
        language = _language_of_header(label)
        if language is not None:
            column_map[letter] = language
    if "zh" not in column_map.values():
        raise ValueError("wide table has no Simplified-Chinese column")
    rows = [
        {column_map[letter]: text for letter, text in cells.items() if letter in column_map}
        for cells in data_rows
    ]
    return rows, column_map


def _norm(text: str) -> str:
    return "".join(text.split())


#: The two wide tables spell their headers differently — the consumer one is
#: `英语预设问题 (en)`, the operator one is `English`. Resolving by the
#: parenthesised tag FIRST covers both without a second table, and covers a
#: future table whose label wording differs again.
_HEADER_TAG = re.compile(r"\(([a-zA-Z][a-zA-Z0-9-]{1,7})\)")
_HEADER_ALIASES = {_norm(label).lower(): tag for label, tag in HEADER_LANGUAGES.items()}
# The consumer table's zh column names no tag; its parenthetical is the English
# gloss of the column ("Original Full Question - ZH").
_HEADER_ALIASES[_norm("原始预设问题 (Original Full Question - ZH)").lower()] = "zh"


def _language_of_header(label: str) -> str | None:
    """The language a header cell names, or ``None`` when it names none we read.

    A cell matching NOTHING is skipped rather than guessed at: a column read as
    the wrong language is worse than a column not read, because the mistake
    lands in the catalog as plausible-looking copy.
    """
    text = label.strip()
    if not text:
        return None
    tag = _HEADER_TAG.search(text)
    if tag and tag.group(1).lower() in set(HEADER_LANGUAGES.values()):
        return tag.group(1).lower()
    # Looked up with the SAME normalisation the keys were built with: the
    # consumer header wraps its gloss onto a second line, so whitespace
    # differs between the file and the alias table.
    return _HEADER_ALIASES.get(_norm(text).lower())


DEFAULT_FAQ_VERSION = "2026.09.12"


def _committed(path: Path) -> str | None:
    """The file's content at HEAD, or ``None`` when it is not in git.

    The baseline for "did the content change" must be what was PUBLISHED, not
    what the tool happened to read: an edit someone made to an input file is
    indistinguishable from the baseline when the tool compares against its own
    input. Only the committed revision answers "is this different from what we
    already published".
    """
    result = subprocess.run(
        ["git", "show", f"HEAD:{path.relative_to(ROOT)}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


#: Languages this tool does NOT translate: they are DERIVED from the authority
#: by a script conversion, so they have no answer file by design. Treating them
#: as missing would make the "a published language lost its answer file" guard
#: fire on every run (#531).
DERIVED_LANGUAGES = frozenset({"zh-Hant"})


def default_languages(answers_dir: Path, prefix: str, wide_table: Path) -> tuple[str, ...]:
    """The languages this platform has BOTH a wide-table column and answers for.

    Derived rather than listed: a fixed default serves whichever platform was
    written first and fails on the other with a FileNotFoundError before it
    reads a single entry.
    """
    rows, column_map = read_wide_table(wide_table)
    available = set(column_map.values()) - {"zh"}
    if not rows:  # pragma: no cover - the table always has rows when valid
        return ()
    return tuple(
        language
        for language in HEADER_LANGUAGES.values()
        if language in available and (answers_dir / f"{prefix}{language}.json").is_file()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=None,
        help=(
            "faq_version to stamp. Defaults to the catalog's CURRENT version, "
            "so a run that changes nothing cannot roll the published revision "
            "backward — the previous literal default predated the data and "
            "would have regressed the version on any plain re-run."
        ),
    )
    parser.add_argument(
        "--xlsx",
        type=Path,
        default=None,
        help="wide table; defaults to the one shipped for --platform",
    )
    parser.add_argument("--platform", default="consumer", choices=("consumer", "operator"))
    parser.add_argument(
        "--languages",
        default=None,
        help=(
            "comma-separated languages to merge. Defaults to the languages this "
            "platform actually has answer files for — a single fixed default "
            "cannot serve two platforms whose translations landed at different "
            "times (operator has vi/mn; consumer has en/de/fr/es/pt)."
        ),
    )
    parser.add_argument(
        "--answers-dir",
        type=Path,
        default=ANSWER_DIR,
        help="directory holding answers_<lang>.json or operator_answers_<lang>.json",
    )
    parser.add_argument(
        "--questions-dir",
        type=Path,
        default=None,
        help=(
            "directory holding questions_<lang>.json for languages the WIDE TABLE has no "
            "column for. The consumer table predates vi/mn/th/km, so their questions have "
            "no column to read; without a second source the merge cannot run for them at "
            "all. Defaults to --answers-dir."
        ),
    )
    args = parser.parse_args()

    wide_table = args.xlsx or WIDE_TABLES[args.platform]
    prefix = ANSWER_PREFIX[args.platform]
    if args.languages:
        langs = tuple(part.strip() for part in args.languages.split(",") if part.strip())
    else:
        langs = default_languages(args.answers_dir, prefix, wide_table)
    if not langs:
        raise ValueError(f"no answer files found for {args.platform} in {args.answers_dir}")

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    version = args.version or str(catalog.get("faq_version") or DEFAULT_FAQ_VERSION)
    entries = catalog["platforms"][args.platform]

    # A default run must not silently DROP a language that is already published.
    # `default_languages` discovers what has an answer file today; if one went
    # missing, merging the rest and preserving the old entries leaves copy that
    # no longer matches its source, and the run says nothing. Removing a
    # language is a deliberate act: name it explicitly in `--languages`.
    if not args.languages:
        published = sorted({lang for entry in entries for lang in (entry.get("i18n") or {})})
        dropped = [lang for lang in published if lang not in langs and lang not in DERIVED_LANGUAGES]
        if dropped:
            raise ValueError(
                f"{args.platform}: 已发布的语言 {dropped} 没有答案文件；"
                "若确实要移除，请在 --languages 里显式列出要保留的语言"
            )
    answers_by_lang = {
        lang: json.loads(
            (args.answers_dir / f"{ANSWER_PREFIX[args.platform]}{lang}.json").read_text(encoding="utf-8")
        )
        for lang in langs
    }
    rows, column_map = read_wide_table(wide_table)
    # A language the wide table HAS a column for is read from the table (the
    # product artifact stays authoritative). One it does NOT — consumer predates
    # vi/mn/th/km — is read from questions_<lang>.json, and a language with
    # NEITHER source is refused rather than merged as an empty question.
    questions_dir = args.questions_dir or args.answers_dir
    table_languages = set(column_map.values())
    questions_by_lang: dict[str, dict[str, str]] = {}
    for lang in langs:
        if lang in table_languages:
            continue
        path = questions_dir / f"{QUESTIONS_PREFIX[args.platform]}{lang}.json"
        if not path.is_file():
            raise ValueError(
                f"{args.platform}: 宽表没有 {lang} 列，也没有 {path.name} —— "
                "该语言的题面没有来源，拒绝合并出空题面"
            )
        questions_by_lang[lang] = json.loads(path.read_text(encoding="utf-8"))
    if len(rows) != len(entries):
        raise ValueError(
            f"wide table has {len(rows)} rows, catalog {args.platform} has {len(entries)} entries"
        )

    for entry, row in zip(entries, rows, strict=True):
        qid = entry["question_id"]
        if _norm(row.get("zh", "")) != _norm(entry["question"]):
            raise ValueError(f"{qid}: zh column drifted from catalog question")
        # MERGE, do not replace: the consumer catalog already carries
        # en/de/fr/es/pt, and re-running this for a second language set must not
        # drop them. A replace was the shape that would silently lose a
        # language the moment a second merge ran.
        # Derived languages are owned by tools/derive_zh_hant.py and are left
        # untouched here — NOT removed. An earlier version popped them, which
        # deleted `zh-Hant` from every entry on every run.
        existing = dict(entry.get("i18n") or {})
        for lang in langs:
            if lang in table_languages:
                question = (row.get(lang) or "").strip()
            else:
                question = (questions_by_lang[lang].get(qid) or "").strip()
            answer = answers_by_lang[lang].get(qid, "").strip()
            if not question or not answer:
                raise ValueError(f"{qid}: missing {lang} question or answer")
            existing[lang] = {"question": question, "answer": answer}
        entry["i18n"] = existing

    recommendations = json.loads(RECOMMENDATIONS.read_text(encoding="utf-8"))
    titles = {entry["question_id"]: entry for entry in entries}
    rec_entries = recommendations["platforms"][args.platform]
    if len(rec_entries) != len(entries):
        raise ValueError(f"faq_recommendations.json {args.platform} count drifted")
    for rec in rec_entries:
        entry = titles[rec["question_id"]]
        merged = dict(rec.get("i18n") or {})
        merged.update({lang: {"title": entry["i18n"][lang]["question"]} for lang in langs})
        rec["i18n"] = merged
    # BOTH files are versioned outputs, so BOTH are part of the verdict — and
    # the baseline is the COMMITTED revision, not what this run read. Comparing
    # against the input made a human's edit to an input file undetectable (it is
    # the input, so it always equals itself), and comparing only the catalog let
    # a recommendation change ship under the old revision. Both are the same
    # mistake: a baseline that cannot see the change being guarded against.
    catalog_changed = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n" != _committed(CATALOG)
    recommendations_changed = json.dumps(recommendations, ensure_ascii=False, indent=2) + "\n" != _committed(
        RECOMMENDATIONS
    )
    if (catalog_changed or recommendations_changed) and args.version is None:
        raise ValueError(
            "内容已变化但没有给 --version：用当前修订号发布新内容，会让按版本号"
            "比较的调用方看不到这次更新。请显式指定新版本。"
        )
    catalog["faq_version"] = version
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    recommendations["faq_version"] = version
    RECOMMENDATIONS.write_text(
        json.dumps(recommendations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "platform": args.platform,
                "languages": list(langs),
                "entries": len(entries),
                "version": version,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
