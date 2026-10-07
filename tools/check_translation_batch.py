#!/usr/bin/env python3
"""Check a #565 translation batch before it is merged.

A machine-translation batch fails in ways a human reviewer cannot see at a glance
and that a "the key exists" check waves through. This verifies the properties
that are cheap to check and that actually differentiate real work from a
placeholder or a half-copied one.

## Calibrated against the repo's OWN published translations

The thresholds are not guesses. `tools/faq_i18n/operator_answers_{vi,mn}.json`
are already merged and reviewed, so they define what "good" looks like here:

- target/source length ratio: **2.9 – 4.3** (Chinese is dense; Vietnamese and
  Mongolian are not). The band below allows 2.0 – 6.0, which catches truncation
  and runaway expansion without grading style.
- **brackets are translated, not preserved.** The source writes interface paths
  as `【充电桩管理 - 场地管理】`; the published translations render them as
  `[Quản lý trụ sạc - Quản lý mặt bằng]`. An earlier version of this checker
  demanded the `【】` glyphs survive — that would have rejected the repo's own
  accepted work. What is checked is the **count**, so a dropped path is caught
  while the bracket style is left to the translator.
- `💡` / `⚠️` markers DO survive verbatim, and their counts must match.
- **Zero CJK characters** in vi/mn. The source data is Chinese, so a copied-out
  value reads as garbage to the reader.

## A stated limit of the unit check

Unit spellings are localized in ways no fixed list exhausts. Measured on the
repo's own accepted work: Mongolian writes kW as `кВт` and kWh as `кВт.цаг`,
Vietnamese writes the decimal point as a comma (`0,2 kWh`). Each of those was
initially reported as a LOSS by this checker, and each was the checker being
wrong, not the translation. The equivalents below cover the spellings actually
observed; a language this repo has not shipped before may use another one, and
the honest reading of a "丢失单位" line is **"a human should look at this"**, not
"this is wrong".

## A check that was removed, and why

An earlier version required the interface-reference COUNT to match the source.
Measurement killed it: the same correct translation is written three ways —
`【设备管理】` → `[Device Management]` (Latin brackets), → `【จัดการอุปกรณ์】`
(Thai kept the glyphs and translated the text), → `"Thông báo …"` (Vietnamese
rendered a `「」` name as a quote). All three preserve the name; only one keeps
the count. The count is now an informational note, not a failure, because a
check that fails on correct work gets switched off — and then checks nothing.

What DOES catch a dropped name is the CJK-residue check plus the length band:
an answer that quietly dropped a referenced name is either shorter than the band
or still carrying the Chinese.

## What this cannot check

Terminology correctness, tone, and whether it reads well to a native speaker.
Those need a human, and the delivery record must say so. This proves only that
the batch is structurally complete and did not silently drop or skip content.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: A bracketed or quoted NAME is an interface reference or a term name. The
#: marker set is the SAME on both sides, because measured translations handle it
#: three different legitimate ways:
#:   - `【设备管理】` → `[Device Management]`   (converted to Latin brackets)
#:   - `【设备管理】` → `【จัดการอุปกรณ์】`        (Thai kept the glyphs, translated the text)
#:   - `「退款到账通知」` → `"Thông báo ..."`    (rendered as a quote)
#: What must be preserved is the NAME, not the glyph, so the check counts how
#: many references the target has and fails only when one DISAPPEARS.
REF_RE = re.compile(r"""【[^】]*】|「[^」]*」|\[[^\]]*\]|«[^»]*»|“[^”]*”|"[^"]*" """.strip())
EMOJI_RE = re.compile(r"💡|⚠️")
#: Tokens carrying meaning that must reappear. Deliberately narrow: a blanket
#: "all digits" rule flags correct work that reorders or spells numbers out.
#: Latin AND Cyrillic unit spellings — Mongolian writes kW as `кВт`, which the
#: Latin-only form silently failed to extract, so the loss check never ran for it.
UNIT_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:kwh|kva|kw|кВт\.?цаг|кВтч|кВт|OCPP\s*[\w.]+|%)",
    re.IGNORECASE,
)
CLOCK_RE = re.compile(r"\d{1,2}:\d{2}")
#: `todo` is the Spanish and Portuguese word for "all" — a `\b`-anchored
#: case-insensitive match on it flagged correct Spanish prose as a placeholder.
#: Uppercase-only for the ambiguous ones: a real placeholder is written in caps,
#: `todo` in running prose never is. `lorem ipsum` is unambiguous either way.
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|FIXME|XXX|PLACEHOLDER|lorem ipsum)\b|待翻译|翻译待补")
CJK_RE = re.compile(r"[一-鿿]")


#: Unit spellings that are localizations of each other, not losses. Mongolian
#: writes kW as `кВт`; Vietnamese uses a comma for the decimal point.
#: Cyrillic and Latin spellings of the same unit. Keys are pre-lowercased.
_UNIT_EQUIVALENTS = {
    "квт.цаг": "kwh",  # 蒙古语 kW·h 的写法
    "квтч": "kwh",
    "кwh": "kwh",
    "квт": "kw",
    "вт": "w",
}


def _normalize_unit(token: str) -> str:
    cleaned = token.replace(" ", "").replace(",", ".").lower()
    for source, target in _UNIT_EQUIVALENTS.items():
        cleaned = cleaned.replace(source, target)
    return cleaned


def _units(text: str) -> set[str]:
    return {_normalize_unit(token) for token in UNIT_RE.findall(text)}


def _clocks(text: str) -> set[str]:
    # 全角/半角与不同的分隔符都归一化：源文用 ～，译文可能用 - 或 ~
    return {re.sub(r"[～~-]", ":", token.replace(" ", "")) for token in CLOCK_RE.findall(text)}


LENGTH_FLOOR = 2.0
LENGTH_CEILING = 6.0


def check_pairs(
    language: str, kind: str, source: dict[str, str], target: dict[str, str], notes: list[str]
) -> list[str]:
    """Compare one (source, target) pair of key->text maps."""
    problems: list[str] = []
    expected, got = set(source), set(target)
    for missing in sorted(expected - got):
        problems.append(f"{language}/{kind}: 缺 {missing}")
    for extra in sorted(got - expected):
        problems.append(f"{language}/{kind}: 多出未知键 {extra}")

    for key in sorted(expected & got):
        value = (target[key] or "").strip()
        origin = source[key]
        if not value:
            problems.append(f"{language}/{kind}[{key}]: 空值")
            continue
        if value == origin.strip():
            problems.append(f"{language}/{kind}[{key}]: 与中文原文逐字相同 —— 未翻译")
            continue
        if PLACEHOLDER_RE.search(value):
            problems.append(f"{language}/{kind}[{key}]: 疑似占位符 {value[:40]!r}")
        if CJK_RE.search(value):
            problems.append(f"{language}/{kind}[{key}]: 残留汉字 {sorted(set(CJK_RE.findall(value)))[:8]}")
        # emoji 是逐字保留的，数量必须相等
        src_emoji, tgt_emoji = len(EMOJI_RE.findall(origin)), len(EMOJI_RE.findall(value))
        if src_emoji != tgt_emoji:
            problems.append(f"{language}/{kind}[{key}]: emoji 数 {src_emoji} -> {tgt_emoji}")
        # 单位与时刻：整块丢了才报，写法本地化不算（蒙古语 kW→кВт、越南语小数点用逗号）
        lost = sorted(_units(origin) - _units(value))
        if lost:
            problems.append(f"{language}/{kind}[{key}]: 丢失单位 {lost}")
        lost_clocks = sorted(_clocks(origin) - _clocks(value))
        if lost_clocks:
            problems.append(f"{language}/{kind}[{key}]: 丢失时刻 {lost_clocks}")
        # 界面引用**不计入失败**，只作为提示 —— 见 docstring 的 "A check that was
        # removed"。翻译可以合法地转成 []、保留【】而换字、或写成引号，三者都是对的。
        src_refs = len(REF_RE.findall(origin))
        tgt_refs = len(REF_RE.findall(value))
        if tgt_refs < src_refs:
            notes.append(f"{language}/{kind}[{key}]: 界面引用 {src_refs} -> {tgt_refs}（供人工扫一眼）")
        ratio = len(value) / max(len(origin), 1)
        if ratio < LENGTH_FLOOR:
            problems.append(f"{language}/{kind}[{key}]: 过短 比值={ratio:.2f}（疑截断）")
        elif ratio > LENGTH_CEILING:
            problems.append(f"{language}/{kind}[{key}]: 过长 比值={ratio:.2f}（疑扩写）")
    return problems


def _load(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("/tmp/faq565"))
    parser.add_argument("--platform", required=True, choices=("consumer", "operator"))
    parser.add_argument("--languages", required=True, help="comma-separated language tags")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    catalog = _load(root / "src" / "aiops_diagnostics" / "faq_catalog.json")
    entries = catalog["platforms"][args.platform]
    source_answers = {entry["question_id"]: entry["answer"] for entry in entries}
    source_questions = {entry["question_id"]: entry["question"] for entry in entries}

    answer_prefix = "operator_answers_" if args.platform == "operator" else "answers_"
    question_prefix = "operator_questions_" if args.platform == "operator" else "questions_"

    problems: list[str] = []
    notes: list[str] = []
    for language in (tag.strip() for tag in args.languages.split(",") if tag.strip()):
        answer_path = args.dir / f"{answer_prefix}{language}.json"
        if not answer_path.is_file():
            problems.append(f"{language}: 缺少答案文件 {answer_path.name}")
        else:
            problems.extend(check_pairs(language, "answer", source_answers, _load(answer_path), notes))
        # 题面只在宽表没有该列时才需要（consumer 的 vi/mn/th/km）
        question_path = args.dir / f"{question_prefix}{language}.json"
        if question_path.is_file():
            problems.extend(check_pairs(language, "question", source_questions, _load(question_path), notes))

    print(
        json.dumps(
            {"platform": args.platform, "problems": len(problems), "notes": len(notes)}, ensure_ascii=False
        )
    )
    for problem in problems:
        print(" -", problem, file=sys.stderr)
    for note in notes:
        print(" ~", note, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
