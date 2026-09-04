#!/usr/bin/env python3
"""从业务 DOCX 生成固定问答目录和前端推荐配置。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
QUESTION = re.compile(r"Q[：:]\s*(.*)")
HEADING = re.compile(r"第.+篇：.*")


def extract_pairs(path: Path) -> list[tuple[str, str]]:
    root = ET.fromstring(ZipFile(path).read("word/document.xml"))
    paragraphs = [
        "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)).strip()
        for paragraph in root.findall(".//w:body/w:p", NS)
    ]
    pairs: list[tuple[str, str]] = []
    for index, paragraph in enumerate(paragraphs):
        match = QUESTION.fullmatch(paragraph)
        if not match:
            continue
        question = match.group(1).strip()
        answer: list[str] = []
        if "A：" in question:
            question, inline_answer = question.split("A：", 1)
            question = question.strip()
            if inline_answer.strip():
                answer.append(inline_answer.strip())
        for following in paragraphs[index + 1 :]:
            if QUESTION.fullmatch(following) or HEADING.fullmatch(following):
                break
            if following:
                answer.append(following.removeprefix("A：").lstrip())
        if question and answer:
            pairs.append((question, "\n".join(answer)))
    return pairs


def build(
    platform: str, source: Path, previous: dict[str, object] | None
) -> tuple[list[dict[str, str]], int, int]:
    previous_entries = (previous or {}).get("platforms", {})
    old = previous_entries.get(platform, []) if isinstance(previous_entries, dict) else []
    old_ids = {item.get("question"): item.get("question_id") for item in old if isinstance(item, dict)}
    used_ids = {item for item in old_ids.values() if isinstance(item, str)}
    next_number = (
        max((int(item.rsplit("q", 1)[-1]) for item in used_ids if re.search(r"q\d+$", item)), default=0) + 1
    )
    entries: list[dict[str, str]] = []
    grouped: dict[str, list[str]] = {}
    for question, answer in extract_pairs(source):
        grouped.setdefault(question, []).append(answer)
    duplicates = 0
    conflicts = 0
    for question, answers in grouped.items():
        unique_answers = list(dict.fromkeys(answers))
        duplicates += len(answers) - len(unique_answers)
        if len(unique_answers) != 1:
            conflicts += 1
            continue
        answer = unique_answers[0]
        question_id = old_ids.get(question)
        if not isinstance(question_id, str) or not question_id.startswith(f"{platform}."):
            question_id = f"{platform}.faq.q{next_number:03d}"
            next_number += 1
        entries.append({"question_id": question_id, "question": question, "answer": answer})
    return entries, duplicates, conflicts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--consumer-docx", type=Path, required=True)
    parser.add_argument("--operator-docx", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("src/aiops_diagnostics"))
    parser.add_argument("--version", default="2026.09.04")
    args = parser.parse_args()
    catalog_path = args.output_dir / "faq_catalog.json"
    previous = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else None
    consumer, consumer_duplicates, consumer_conflicts = build("consumer", args.consumer_docx, previous)
    operator, operator_duplicates, operator_conflicts = build("operator", args.operator_docx, previous)
    catalog = {
        "faq_version": args.version,
        "platforms": {"consumer": consumer, "operator": operator},
        "sources": {
            "consumer": {
                "file": args.consumer_docx.name,
                "sha256": hashlib.sha256(args.consumer_docx.read_bytes()).hexdigest(),
            },
            "operator": {
                "file": args.operator_docx.name,
                "sha256": hashlib.sha256(args.operator_docx.read_bytes()).hexdigest(),
            },
        },
    }
    recommendations = {
        "faq_version": args.version,
        "platforms": {
            platform: [
                {"question_id": entry["question_id"], "title": entry["question"], "sort": index}
                for index, entry in enumerate(entries, 1)
            ]
            for platform, entries in (("consumer", consumer), ("operator", operator))
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "faq_recommendations.json").write_text(
        json.dumps(recommendations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "consumer": len(consumer),
                "operator": len(operator),
                "duplicates": consumer_duplicates + operator_duplicates,
                "conflicts": consumer_conflicts + operator_conflicts,
                "version": args.version,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
