"""The L1 index and the numbers the baseline quotes off it (#476).

The L1 layer makes claims of exactly two kinds, and both have already been the
source of a wrong conclusion once in this repository's history:

* **every repository is listed** — a row dropped at a page boundary is
  invisible in the table. The procedure's §1 records why the tool pages to the
  end; this test is what proves the generated index actually has all of them.
* **a branch's file count means what the document says it means** — the
  "default branch is a scaffold" reading came from counting one branch and
  never looking at the other. The live half re-counts the two branches the
  document leans on, and asserts the *relationship* the document asserts
  (scaffold vs real code, an order of magnitude apart) rather than a number
  that moves with every commit.

The generated index is checked structurally on every run; the live half is
skipped without an identity anchor, like the procedure's own evidence test.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md"
INDEX = PROJECT_ROOT / "docs" / "agents" / "company-repo-index.md"
GENERATOR = PROJECT_ROOT / "tools" / "company_repo_index.py"
SCRIPT = "deploy/company-gitlab-api.sh"

#: The domains the generator's rules can emit. `未定` is a real answer here, not
#: a failure — the rule is mechanical and refuses to guess. Anything outside
#: this set means the rule changed and the document was not updated with it.
DOMAINS = {"充电桩", "商城", "UPMS", "网关", "认证", "通用库", "未定"}

_ROW = re.compile(
    r"^\|\s*`(?P<path>[^`]+)`\s*\|\s*(?P<id>\d+)\s*\|\s*`(?P<branch>[^`]*)`"
    r"\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<domain>\S+)\s*\|\s*$"
)


def _rows() -> list[dict[str, str]]:
    out = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            out.append(m.groupdict())
    return out


def test_the_generator_and_its_output_are_both_present() -> None:
    assert GENERATOR.exists()
    assert INDEX.exists()
    assert BASELINE.exists()


def test_the_index_is_generated_not_hand_written() -> None:
    """The header says so; if it stops saying so, the table starts drifting."""
    head = "\n".join(INDEX.read_text(encoding="utf-8").splitlines()[:12])
    assert "tools/company_repo_index.py" in head
    assert "不要手改" in head
    # The snapshot warning is the difference between "this is the state on a
    # date" and "this is the state" — the document must keep saying which.
    assert "快照" in head


def test_every_row_parses_and_the_set_is_complete() -> None:
    rows = _rows()
    assert len(rows) == 519, f"索引有 {len(rows)} 行，预期 519（分页漏了？）"
    by_id = {r["id"] for r in rows}
    assert len(by_id) == len(rows), "同一个仓出现了两次 —— 分页把某一页取重了"
    assert all(r["path"] for r in rows)
    assert all(r["domain"] in DOMAINS for r in rows), "域列出现了生成器规则产不出的值：" + repr(
        {r["domain"] for r in rows} - DOMAINS
    )


def test_the_activity_buckets_in_the_baseline_match_the_index() -> None:
    """§0 quotes four counts; they have to be the index's own numbers."""
    import datetime

    text = BASELINE.read_text(encoding="utf-8")
    rows = _rows()
    # The index is stamped with its fetch date; bucket against that, not today,
    # or the numbers drift as the calendar moves and the doc looks wrong when
    # it is only old.
    stamped = re.search(r"取数日 \*\*(\d{4}-\d{2}-\d{2})\*\*", text)
    index_day = re.search(r"取数日 \*\*(\d{4}-\d{2}-\d{2})\*\*", INDEX.read_text(encoding="utf-8"))
    assert stamped and index_day, "基线或索引缺「取数日」"
    day = datetime.date.fromisoformat(index_day.group(1))

    # Date granularity, matching the column the index prints. ISO date strings
    # parse to midnight, so this is the same number a reader gets by hand.
    buckets: Counter[str] = Counter()
    for r in rows:
        age = (day - datetime.date.fromisoformat(r["date"])).days
        buckets["<=30" if age <= 30 else "31-180" if age <= 180 else "181-365" if age <= 365 else ">365"] += 1

    # Bold is stylistic (the document emphasises two of the four); the parse
    # accepts either so that changing emphasis is not a test failure.
    quoted = {
        m.group("label"): int(m.group("n"))
        for m in re.finditer(
            r"\|\s*(?P<label>≤ 30 天（活）|31–180 天（半活）|181–365 天（慢）|> 365 天（停）)"
            r"\s*\|\s*\*{0,2}(?P<n>\d+)\*{0,2}\s*\|",
            text,
        )
    }
    assert len(quoted) == 4, f"§0 的活跃度表只解析出 {len(quoted)} 行 —— 它是本层唯一的量级说明"
    expected = {
        "≤ 30 天（活）": buckets["<=30"],
        "31–180 天（半活）": buckets["31-180"],
        "181–365 天（慢）": buckets["181-365"],
        "> 365 天（停）": buckets[">365"],
    }
    assert quoted == expected, f"§0 与索引对不上：文档 {quoted}，索引 {expected}"


def test_the_unclassified_count_in_the_baseline_matches_the_index() -> None:
    text = BASELINE.read_text(encoding="utf-8")
    rows = _rows()
    unclassified = sum(1 for r in rows if r["domain"] == "未定")
    m = re.search(r"全索引里 \*\*(\d+) / (\d+)\*\* 行", text)
    assert m, "§4 不再写明未定有多少行 —— 那是「不猜」这条纪律的证据"
    assert (int(m.group(1)), int(m.group(2))) == (unclassified, len(rows))


def _live() -> bool:
    ca = os.environ.get("AIOPS_GL_CA")
    return bool(ca and Path(ca).exists())


_live_only = pytest.mark.skipif(
    not _live(), reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核"
)


def _java(project: str, ref: str) -> int:
    proc = subprocess.run(
        ["bash", SCRIPT, "tree", "--project", project, "--ref", ref, "--count-java"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    return int(re.search(r"java=(\d+)", proc.stdout).group(1))


@_live_only
def test_the_default_branch_really_is_a_scaffold_for_charging_pile() -> None:
    """The one claim §1 rests on, asserted as a relationship not a number.

    "34 vs 2012" is a measurement that moves; "the default branch is at least
    an order of magnitude thinner than the real one" is the finding.
    """
    master = _java("363", "master")
    release = _java("363", "release")
    assert release >= master * 10, (
        f"默认分支不再是脚手架了（master={master}, release={release}）—— "
        "L1 与规程 §2 的反例需要重新核对，不要直接改数字。"
    )


def test_a_repository_matching_two_domains_stays_unclassified() -> None:
    """Cross-domain hits are `未定`, not the first rule that happens to match.

    `cloud-mall-common` matches 商城 (`mall`) and 通用库 (`common`); taking the
    first match turned a genuinely undecided repository into a confident
    "商城" in the index while the baseline said 待定 — two entry points giving
    contradictory answers about the same repository.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("repo_index", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.classify("s2b2c-java/cloud-mall-common") == "未定"
    # The namespace must not do keyword duty: `bladex/` contains `blade`, and
    # matching the whole path made every repository under it a 通用库.
    assert mod.classify("bladex/old") == "未定"
    assert mod.classify("bladex/meidi") == "未定"
    # …while a repository that really is one domain still classifies.
    assert mod.classify("s2b2c-java/cloud-upms") == "UPMS"
    assert mod.classify("iot/cloud-charging-pile") == "充电桩"


def test_an_empty_repository_does_not_get_a_branch_named_null() -> None:
    """GitLab sends JSON null, which the shell prints as the text `null`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("repo_index", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._branch("null") is None
    assert mod._branch("") is None
    assert mod._branch("main") == "main"
