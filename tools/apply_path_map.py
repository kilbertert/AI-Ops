#!/usr/bin/env python3
"""Apply a translated interface-path map to a batch's answer files (#565).

The first pass of the #565 batch preserved the interface paths verbatim, which
left **~470 Han characters per file** in en/de/es/pt — the same defect the repo's
own published vi/mn translations do not have (they contain none). Translating 59
unique paths once per language is far cheaper than re-translating 17 long answers,
so this rewrites only the bracketed names.

It fails loudly rather than silently leaving a path behind: a name with no
mapping is reported, because that is exactly the residue this script exists to
remove, and it would otherwise ship as Chinese text inside a German answer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PATH_RE = re.compile(r"【[^】]*】")
CJK_RE = re.compile(r"[一-鿿]")


def apply_map(text: str, mapping: dict[str, str], missing: set[str]) -> str:
    def swap(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = mapping.get(original)
        if replacement is None:
            missing.add(original)
            return original
        return replacement

    return PATH_RE.sub(swap, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("/tmp/faq565"))
    parser.add_argument("--language", required=True)
    parser.add_argument(
        "--file", required=True, help="answer file name in --dir, e.g. operator_answers_de.json"
    )
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None, help="defaults to in-place")
    args = parser.parse_args()

    mapping = json.loads(args.map.read_text(encoding="utf-8"))
    source = args.dir / args.file
    payload = json.loads(source.read_text(encoding="utf-8"))

    missing: set[str] = set()
    updated = {key: apply_map(value, mapping, missing) for key, value in payload.items()}

    if missing:
        print(f"未映射的界面路径 {len(missing)} 条：", file=sys.stderr)
        for item in sorted(missing):
            print("  -", item, file=sys.stderr)
        return 1

    before = sum(len(CJK_RE.findall(value)) for value in payload.values())
    after = sum(len(CJK_RE.findall(value)) for value in updated.values())
    target = args.out or source
    target.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {"language": args.language, "file": str(target), "cjk_before": before, "cjk_after": after},
            ensure_ascii=False,
        )
    )
    # 汉字清零才是成功；留一个也说明有路径没译
    return 1 if after else 0


if __name__ == "__main__":
    raise SystemExit(main())
