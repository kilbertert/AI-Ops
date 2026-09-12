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
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "aiops_diagnostics" / "faq_catalog.json"
RECOMMENDATIONS = ROOT / "src" / "aiops_diagnostics" / "faq_recommendations.json"
WIDE_TABLE = ROOT / "docs" / "EV_Charging_FAQ_Multilingual_Wide_Table.xlsx"
ANSWER_DIR = ROOT / "tools" / "faq_i18n"

LANGS = ("en", "de", "fr", "es", "pt")
COLUMNS = {"A": "zh", "B": "en", "C": "de", "D": "fr", "E": "es", "F": "pt"}

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


def read_wide_table(path: Path) -> list[dict[str, str]]:
    """Return rows of {lang: text} with header validation; row 1 is the header."""
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        sheet = ET.fromstring(zf.read(_first_sheet_path(zf)))
    rows: list[dict[str, str]] = []
    for row in sheet.iter(f"{{{_NS_MAIN}}}row"):
        cells: dict[str, str] = {}
        for cell in row.findall(f"{{{_NS_MAIN}}}c"):
            column = _column_index(cell.attrib.get("r", ""))
            if column in COLUMNS:
                cells[COLUMNS[column]] = _cell_text(cell, shared)
        rows.append(cells)
    header, data_rows = rows[0], rows[1:]
    missing = [lang for lang in ("zh", *LANGS) if lang not in header]
    if missing:
        raise ValueError(f"wide table header misses columns: {missing}")
    return data_rows


def _norm(text: str) -> str:
    return "".join(text.split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="2026.09.12", help="new faq_version")
    parser.add_argument("--xlsx", type=Path, default=WIDE_TABLE)
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    consumers = catalog["platforms"]["consumer"]
    answers_by_lang = {
        lang: json.loads((ANSWER_DIR / f"answers_{lang}.json").read_text(encoding="utf-8")) for lang in LANGS
    }
    rows = read_wide_table(args.xlsx)
    if len(rows) != len(consumers):
        raise ValueError(f"wide table has {len(rows)} rows, catalog has {len(consumers)} entries")

    for entry, row in zip(consumers, rows, strict=True):
        qid = entry["question_id"]
        if _norm(row.get("zh", "")) != _norm(entry["question"]):
            raise ValueError(f"{qid}: zh column drifted from catalog question")
        i18n: dict[str, dict[str, str]] = {}
        for lang in LANGS:
            question = (row.get(lang) or "").strip()
            answer = answers_by_lang[lang].get(qid, "").strip()
            if not question or not answer:
                raise ValueError(f"{qid}: missing {lang} question or answer")
            i18n[lang] = {"question": question, "answer": answer}
        entry["i18n"] = i18n

    catalog["faq_version"] = args.version
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    recommendations = json.loads(RECOMMENDATIONS.read_text(encoding="utf-8"))
    titles = {entry["question_id"]: entry for entry in consumers}
    rec_entries = recommendations["platforms"]["consumer"]
    if len(rec_entries) != len(consumers):
        raise ValueError("faq_recommendations.json consumer count drifted")
    for rec in rec_entries:
        entry = titles[rec["question_id"]]
        rec["i18n"] = {lang: {"title": entry["i18n"][lang]["question"]} for lang in LANGS}
    recommendations["faq_version"] = args.version
    RECOMMENDATIONS.write_text(
        json.dumps(recommendations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"consumer": len(consumers), "languages": list(LANGS), "version": args.version},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
