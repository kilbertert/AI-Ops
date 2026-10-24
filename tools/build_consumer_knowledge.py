#!/usr/bin/env python3
"""将运营工作簿编译为私有、可复核的 WF01 内容包；不发布或调用业务。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

from merge_faq_i18n import read_sheet_table

WRITE_TOOLS = frozenset(
    {
        "start_session",
        "order_create",
        "stop_session",
        "privacy_request",
        "safety_event_create",
        "create_ticket",
    }
)
SHEETS = {
    "faq": "C端FAQ知识库",
    "media": "多媒体素材清单",
    "intents": "意图与API映射",
    "escalation": "转人工与安全规则",
    "configuration": "上线配置清单",
}
REQUIRED = {
    "KB_ID",
    "一级分类",
    "意图",
    "建设优先级",
    "适用角色",
    "用户常见问法/同义词",
    "标准答案（中文）",
    "标准答案（English）",
    "实时数据/API依赖",
    "转人工条件",
    "风险与禁止承诺",
    "客户可配置项",
    "发布状态",
    "版本",
}


def refs(value: str) -> set[str]:
    result = set()
    for term in re.split(r"[;；]", value):
        term = term.strip()
        if not term:
            continue
        match = re.fullmatch(r"KB-C-(\d{3})~(\d{3})", term)
        if match:
            start, end = map(int, match.groups())
            if start > end:
                raise ValueError("invalid FAQ range")
            result.update(f"KB-C-{n:03}" for n in range(start, end + 1))
        elif re.fullmatch(r"(?:KB-C|IMG|VID)-\d{3}", term):
            result.add(term)
        else:
            raise ValueError(f"invalid reference: {term}")
    return result


def indexed(rows: list[dict], key: str, pattern: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        value = row.get(key, "")
        if not re.fullmatch(pattern, value) or value in result:
            raise ValueError(f"invalid or duplicate {key}: {value}")
        result[value] = row
    if not result:
        raise ValueError(f"empty table: {key}")
    return result


def compile_package(source: Path) -> dict:
    workbooks = list(source.glob("*.xlsx"))
    if len(workbooks) != 1:
        raise ValueError("exactly one workbook required")
    tables = {key: read_sheet_table(workbooks[0], name) for key, name in SHEETS.items()}
    faq = indexed(tables["faq"], "KB_ID", r"KB-C-\d{3}")
    media = indexed(tables["media"], "素材ID", r"(?:IMG|VID)-\d{3}")
    for key, field, pattern in [
        ("intents", "意图ID", r"INT-\d{2}"),
        ("escalation", "规则ID", r"ESC-\d{3}"),
        ("configuration", "配置ID", r"CFG-\d{2}"),
    ]:
        indexed(tables[key], field, pattern)
    with (source / "MEDIA_ASSET_MANIFEST.csv").open(encoding="utf-8-sig", newline="") as f:
        if list(csv.DictReader(f)) != tables["media"]:
            raise ValueError("media CSV differs from workbook")
    markdown = {}
    for path in sorted((source / "knowledge_markdown").glob("*.md")):
        for match in re.finditer(r"^## (KB-C-\d{3}) \| (.+)\n([\s\S]*?)(?=^## |\Z)", path.read_text(), re.M):
            key, title, body = match.groups()
            if key in markdown:
                raise ValueError(f"duplicate Markdown FAQ: {key}")
            markdown[key] = (title, body, str(path.relative_to(source)))
    if set(markdown) != set(faq):
        raise ValueError("Markdown/workbook FAQ IDs differ")
    for key, row in faq.items():
        if any(not row.get(field, "").strip() for field in REQUIRED):
            raise ValueError(f"required FAQ field missing: {key}")
        for label, field in [
            ("标准答复（中文）", "标准答案（中文）"),
            ("Standard answer (English)", "标准答案（English）"),
        ]:
            match = re.search(
                r"\*\*" + re.escape(label) + r"\*\*\s*\n(.*?)(?=\n\*\*|\Z)", markdown[key][1], re.S
            )
            if not match or match[1].strip() != row[field].strip():
                raise ValueError(f"Markdown answer differs: {key}/{field}")
        match = re.search(r"\*\*关联媒体素材 / Media assets:\*\*([^\n]*)", markdown[key][1])
        linked = refs(row.get("关联媒体素材ID", ""))
        if linked != refs(match[1] if match else "") or linked - media.keys():
            raise ValueError(f"invalid media references: {key}")
    for key, row in media.items():
        if refs(row["关联FAQ_ID"]) - faq.keys():
            raise ValueError(f"unknown FAQ in media: {key}")
    tools = set()
    for row in tables["intents"]:
        if refs(row["关联FAQ_ID"]) - faq.keys():
            raise ValueError("unknown FAQ in intent mapping")
        tools.update(t.strip() for t in row["建议工具/API能力（逻辑名）"].split(";") if t.strip())
    prompt = read_sheet_table(workbooks[0], "客服Prompt")
    if len(prompt) != 1 or prompt[0]["内容"] not in (source / "00_System_Prompt.md").read_text():
        raise ValueError("system prompt differs from workbook")
    # 两边声明都保留；不擅自把反向清单的规划关系提升为问答的已生效媒体。
    forward = {(key, m) for key, row in faq.items() for m in refs(row.get("关联媒体素材ID", ""))}
    reverse = {(f, key) for key, row in media.items() for f in refs(row["关联FAQ_ID"])}
    conflicts = [
        {"faq_id": f, "asset_id": m, "faq_declares": (f, m) in forward, "media_declares": (f, m) in reverse}
        for f, m in sorted(forward ^ reverse)
    ]
    entries = []
    for key, row in faq.items():
        entries.append(
            {
                "faq_id": key,
                "title": markdown[key][0],
                "category": row["一级分类"],
                "query_variants": [
                    q.strip() for q in re.split("[;；]", row["用户常见问法/同义词"]) if q.strip()
                ],
                "answers": {"zh": row["标准答案（中文）"], "en": row["标准答案（English）"]},
                "source": markdown[key][2],
                "source_status": row["发布状态"],
                "version": row["版本"],
                "api_dependency": row["实时数据/API依赖"],
                "tenant_configuration": row["客户可配置项"],
                "escalation_condition": row["转人工条件"],
                "prohibited_claims": row["风险与禁止承诺"],
                "media_candidates": sorted(refs(row.get("关联媒体素材ID", ""))),
                "related_intents": [r["意图ID"] for r in tables["intents"] if key in refs(r["关联FAQ_ID"])],
            }
        )
    files = [
        workbooks[0],
        source / "MEDIA_ASSET_MANIFEST.csv",
        source / "00_System_Prompt.md",
        *sorted((source / "knowledge_markdown").glob("*.md")),
    ]
    return {
        "schema": "aiops-consumer-content/1",
        "status": "draft",
        "entries": entries,
        "source_faq_rows": tables["faq"],
        "source_system_prompt": prompt[0]["内容"],
        "source_sha256": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
        "media_manifest": tables["media"],
        "media_conflicts": conflicts,
        "intents": tables["intents"],
        "escalation_rules": tables["escalation"],
        "configuration": tables["configuration"],
        "workflow_scope": {
            "write_tools_disabled": sorted(tools & WRITE_TOOLS),
            "read_candidates_unverified": sorted(tools - WRITE_TOOLS),
        },
        "summary": {
            "faqs": len(entries),
            "categories": len({r["category"] for r in entries}),
            "query_variants": sum(len(r["query_variants"]) for r in entries),
            "media_planned": len(media),
            "media_with_address": sum(bool(r["实际文件URL/存储地址"]) for r in media.values()),
            "media_conflicts": len(conflicts),
            "publishable": False,
        },
    }


def write_bundle(source: Path, output: Path) -> dict:
    bundle = compile_package(source)
    if output.exists():
        raise ValueError("output must be a new directory; existing evidence is preserved")
    # 校验完成后才写；不能产生一个被误认成可导入的半成品。
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".consumer-content-", dir=output.parent) as staging:
        root = Path(staging)
        for lang in ("zh", "en"):
            (root / lang).mkdir(mode=0o700)
        for row in bundle["entries"]:
            for lang, answer in row["answers"].items():
                text = f"# {row['faq_id']} | {row['title']}\n\n{answer}\n"
                path = root / lang / f"{row['faq_id']}.md"
                path.write_text(text, encoding="utf-8")
                path.chmod(0o600)
        path = root / "content.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        path = root / "README.md"
        path.write_text(
            "# WF01 待审核内容\n\n本目录为草稿，禁止直接作为正式知识发布。\n"
            "中文/英文正文不含内部工具名、媒体ID或后台升级规则。\n"
            "原始状态、API依赖、配置缺口和媒体冲突均在 content.json。\n"
            "媒体地址存在也不代表可用或已审核，本工具不签发任何媒体。\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        root.rename(output)
    return bundle["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = write_bundle(args.source, args.output)
    except (ValueError, OSError, KeyError) as exc:
        print(f"content package refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
