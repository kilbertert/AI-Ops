#!/usr/bin/env python3
"""Ask the production access log what the deployed client actually calls.

Why this exists: when a frontend feature is missing, there are two very
different explanations and the symptom looks identical —

  1. the client never asks for it, or
  2. the client asks and the call fails.

`grep -c` on the access log separates them, and it is the ONLY cheap evidence
that does. Every other route (reading the frontend repo, asking the frontend
team, reasoning from the API contract) is slower and answers a different
question. This was the step that explained a vanished shortcut row after
several hypotheses that all fit the symptom.

Read-only: it never writes, and it never prints a credential.
"""

from __future__ import annotations

import argparse
import collections
import re
import subprocess

#: Access-log lines look like:
#:   ip - - [07/Oct/2026:15:20:08 +0800] "GET /v1/faq/answer HTTP/1.1" 200 938 "-" "UA"
LINE = re.compile(
    r"^(?P<ip>\S+).*?\[(?P<ts>\d{2}/[A-Za-z]{3}/\d{4}):[^\]]+\]\s+"
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+"\s+(?P<status>\d{3})\s+(?P<size>\d+)'
    r'(?:\s+"[^"]*"\s+"(?P<ua>[^"]*)")?'
)


def _read(path: str, since: str | None) -> str:
    # The log is mode 700 www:www on 41, so this runs on the host. `--since` is
    # a plain substring match on the date field ("07/Oct/2026").
    pattern = since or ""
    command = ["cat", path] if not pattern else ["grep", "-F", pattern, path]
    return subprocess.run(command, capture_output=True, text=True, check=False).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="access log path (on the host that owns it)")
    parser.add_argument("--since", help='date substring, e.g. "07/Oct/2026"')
    parser.add_argument("--path-prefix", default="/v1/", help="only report paths under this prefix")
    parser.add_argument("--ua", help="only report lines whose User-Agent contains this")
    parser.add_argument("--missing", help="report this path's request count and FAIL LOUDLY if zero")
    args = parser.parse_args()

    text = _read(args.log, args.since)
    counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
    total = 0
    for line in text.splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        if args.ua and args.ua not in (match.group("ua") or ""):
            continue
        path = match.group("path")
        if not path.startswith(args.path_prefix):
            continue
        total += 1
        counts[(match.group("method"), path, match.group("status"))] += 1

    print(f"匹配 {total} 行（{args.log}{' since ' + args.since if args.since else ''}）")
    for (method, path, status), count in sorted(counts.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {count:6}  {method:4} {path}  {status}")

    if args.missing:
        hits = sum(count for (_, path, _), count in counts.items() if path == args.missing)
        print()
        if hits:
            print(f"✅ {args.missing} 有 {hits} 次请求")
            return 0
        print(f"❌ {args.missing} 在窗口内 0 次请求")
        print("   ⇒ 客户端根本没调它。这与「调了但失败」是两个不同的结论：")
        print("     失败会留下 4xx/5xx 行，**一行都没有**说明请求没发出去。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
