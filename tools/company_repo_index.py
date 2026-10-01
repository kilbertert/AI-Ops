#!/usr/bin/env python3
"""Render the company repository index (#476, the L1 layer of #414).

The index is 519 rows and it is a *reading* aid, not an assertion: what it
records is the repository's own metadata (id, default branch, last activity)
plus one classification this script makes from the path alone.

Two decisions are worth stating, because both are the kind that quietly turns
a useful index into a misleading one:

* **The classification is mechanical.** It reads the namespace prefix and the
  repository name; it never guesses from what a repository sounds like it does.
  Anything the rule does not recognise is labelled ``未定`` and stays that way —
  a wrong domain is worse than no domain, because the next reader stops
  checking.
* **``last_activity_at`` is a snapshot, not a contract.** It is stamped with the
  time it was fetched. A repository that goes quiet after that is not reflected
  here, and the field says so in the output.

Fetching is done through ``deploy/company-gitlab-api.sh``: same credentials,
same pinned certificate, same read-only surface as every other recon step. This
script does not talk to the network itself.

    python3 tools/company_repo_index.py > docs/agents/company-repo-index.md
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API = "deploy/company-gitlab-api.sh"

#: Domain rules, in order. A row matches when its namespace prefix matches AND
#: its repository name contains one of the keywords. Both halves are needed:
#: the prefix alone would swallow every repository in `s2b2c-java/`, and the
#: keyword alone would pull in forks under personal namespaces.
RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "充电桩",
        ("iot/", "iot-app/", "iot-front/"),
        (
            "charging",
            "pile",
            "ocpp",
            "tcp-biz",
            "jy-biz",
            "vehicle",
            "barrier",
            "qssdb",
            "tsdata",
            "jetlinks",
        ),
    ),
    (
        "商城",
        ("s2b2c-java/", "s2b2c-front/", "s2b2c/", "goods_market/"),
        (
            "qumall",
            "mall",
            "shop",
            "goods",
            "diy",
            "order",
            "member",
            "invoice",
            "inventory",
            "rent",
            "pay",
            "data",
            "sdk",
        ),
    ),
    ("UPMS", ("s2b2c-java/",), ("upms",)),
    ("网关", ("s2b2c-java/",), ("gateway",)),
    ("认证", ("s2b2c-java/",), ("auth",)),
    (
        "通用库",
        ("s2b2c-java/", "common-java/", "paas/", "bladex/", "base/", "java/"),
        ("common", "sdk", "starter", "blade", "ddd4j", "message", "qushisdk"),
    ),
)

UNCLASSIFIED = "未定"


def fetch_projects() -> list[dict]:
    """All visible projects, newest activity first.

    ``order_by=last_activity_at`` is applied server-side; the script pages to
    the end, so nothing is silently dropped at a page boundary.
    """
    proc = subprocess.run(
        ["bash", API, "projects"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"{API} projects 失败：{proc.stderr}")
    rows = []
    for line in proc.stdout.splitlines():
        # The script prints `id \t path \t default=<branch> \t activity=<iso>`;
        # the two labelled fields are parsed by label, not by position, so a
        # missing default branch (GitLab returns null for an empty repository)
        # cannot shift the activity date into the branch column.
        pid, path, rest = line.split("\t", 2)
        fields = dict(part.split("=", 1) for part in rest.split("\t"))
        rows.append(
            {
                "id": int(pid),
                "path_with_namespace": path,
                "default_branch": fields.get("default") or None,
                "last_activity_at": fields.get("activity", ""),
            }
        )
    return rows


def classify(path_with_namespace: str) -> str:
    path = path_with_namespace.lower()
    for domain, namespaces, keywords in RULES:
        if any(path.startswith(ns) for ns in namespaces) and any(k in path for k in keywords):
            return domain
    return UNCLASSIFIED


def main() -> None:
    projects = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else fetch_projects()
    stamped = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    projects.sort(key=lambda r: r["path_with_namespace"].lower())

    print("# 公司仓库索引（L1）")
    print()
    print(f"> 由 `tools/company_repo_index.py` 生成，**不要手改**。取数日 **{stamped}**（UTC）。")
    print(">")
    print("> 三列来自公司 GitLab 自己的元数据（`id` / `default_branch` / `last_activity_at`）；")
    print(f"> 末列「域」是本脚本按**路径**机械分类的结果，不认识的一律 `{UNCLASSIFIED}`。")
    print(">")
    print("> ⚠️ `last_activity_at` 是**取数时点的快照，不是契约** —— 之后变安静的仓不会反映在这里。")
    print("> 要判断「现在还在动吗」，重跑脚本，不要引用本表的日期。")
    print(">")
    print("> ⚠️ **默认分支不等于真分支**（#475 规程 §2）。本表只记默认分支；")
    print("> 每个域要用的那个分支写在各域的条目里，判据是当场跑出来的 `.java` 计数。")
    print()
    print("| 仓（`path_with_namespace`） | id | 默认分支 | 最后活动 (UTC) | 域 |")
    print("|---|---|---|---|---|")
    for r in projects:
        default = r.get("default_branch") or "—"
        print(
            f"| `{r['path_with_namespace']}` | {r['id']} | `{default}` "
            f"| {r['last_activity_at'][:10]} | {classify(r['path_with_namespace'])} |"
        )


if __name__ == "__main__":
    main()
