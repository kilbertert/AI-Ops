"""L4 proves the baseline is usable, not that it is long (#479).

The L4 section's own rule is the thing worth testing: it claims *the chain runs
inside the baseline* — every step points back at an anchor that already exists
(`L2-N` / `L3-N`), and nothing was looked up fresh to get around the baseline.
Those are checkable claims:

* every `L2-N` / `L3-N` an L4 step points at must be a heading that exists;
* the chain must be a chain — each step naming an anchor, not free prose;
* the coverage gap must keep its four parts and the two `fail closed` ones,
  because collapsing them into one bucket is the mistake the section warns about;
* the deviation must be recorded as a pending item **with an owner that can
  actually pick it up** — D batch finished on 2026-09-30 and only changed the
  inbound hop, so pointing at it would mean nobody does;
* the section must state what it did *not* do (rebuild it, re-measure the
  production database) so a reader does not take the quoted numbers for fresh.

The live half re-reads the two files the chain's endpoints rest on.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md"
SCRIPT = "deploy/company-gitlab-api.sh"

#: An anchor into another layer of this document.
_ANCHOR = re.compile(r"\bL([23])-(\d)\b")


def _section(name: str) -> str:
    text = BASELINE.read_text(encoding="utf-8")
    start = text.index(f"## {name} ·")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _tail(part: str, start: str) -> str:
    return part.split(f"### {start} ")[1]


def _headings(layer: str) -> set[str]:
    """Heading ids of one layer. `layer` is `2` or `3` — the digit, not `L2`."""
    return set(re.findall(rf"^### (L{layer}-\d) ", _section(f"L{layer}"), re.MULTILINE))


def test_l4_is_present_and_filled() -> None:
    l4 = _section("L4")
    assert "（尚未填充。）" not in l4, "L4 还是占位符"
    assert "L4-0" in l4 and "L4-1" in l4


def test_the_chain_points_only_at_anchors_that_exist() -> None:
    """A reference to `L2-7` means nothing if there is no such heading."""
    l4 = _section("L4")
    chain = _tail(l4, "L4-1").split("**这条链")[0]
    refs = {m.groups() for m in re.finditer(r"\bL([23])-(\d)\b", chain)}
    assert refs, "L4-1 的引用链一条锚点都没有 —— 那就不是引用链"
    for layer, digit in sorted(refs):
        ref = f"L{layer}-{digit}"
        assert ref in _headings(layer), f"{ref} 在 L{layer} 里没有对应的标题"
    assert {layer for layer, _ in refs} == {"2", "3"}, (
        "引用链没有同时用到 L2 与 L3 —— L4 的意义就是把它们串起来"
    )


def test_the_chain_is_a_table_of_steps() -> None:
    """Free prose would hide a step that had no anchor."""
    l4 = _section("L4")
    # Cut at the next `### ` heading: `_tail` runs to the end of the section, so
    # without this the coverage table from L4-2 would be read as chain rows.
    chain = _tail(l4, "L4-1").split("\n### ")[0]
    table = chain.split("| 步 |")[1]
    rows = [r for r in table.splitlines() if r.startswith("|") and r.count("|") >= 3]
    assert len(rows) >= 7, f"引用链只有 {len(rows)} 行（含表头）—— 它应当是逐步骤的表"
    # Every step row must name where it came from.
    for row in rows:
        if set(row.replace("|", "").strip()) <= set("-: "):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if cells[0] == "步":
            continue
        # The anchor must be a real one. `startswith("L")` was the earlier
        # fallback and it accepted `Later` — a step could lose its anchor and
        # still pass because other steps kept theirs.
        assert _ANCHOR.search(cells[-1]), f"这一步没有指回 L2/L3：{row[:90]}"


def _gap_rows() -> dict[str, str]:
    """The L4-2 table keyed by class letter (B/C/D/E)."""
    gap = _tail(_section("L4"), "L4-2").split("\n### ")[0]
    out: dict[str, str] = {}
    for row in gap.splitlines():
        if not row.startswith("|") or row.count("|") < 4:
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        m = re.match(r"\*\*([A-E])\*\*", cells[0])
        if m:
            out[m.group(1)] = row
    return out


def test_the_coverage_gap_keeps_its_four_parts() -> None:
    """B/C/D/E, with C and D marked fail-closed — not one "31.7%" bucket.

    Each row is checked on its own: a count appearing *somewhere* in the
    subsection is not the same as that class having a verdict, and counting
    `fail closed` occurrences would pass while the two rows that need it lost it.
    """
    l4 = _section("L4")
    gap = _tail(l4, "L4-2")
    rows = _gap_rows()
    assert set(rows) == {"B", "C", "D", "E"}, f"覆盖缺口的分类行不全：{sorted(rows)}"
    for part, count in (("B", "9,307"), ("C", "57"), ("D", "297"), ("E", "20,782")):
        assert count in rows[part], f"{part} 类的行里没有它的订单数（{count}）"

    # The verdict lives with the class it applies to.
    assert "fail closed" in rows["C"] and "fail closed" in rows["D"], (
        "fail closed 必须写在 C、D 各自的**那一行**里 —— 写在别处读者不会把它与类别对应起来"
    )
    assert "fail closed" not in rows["B"] and "fail closed" not in rows["E"], (
        "B 是正常类别、E 是正常路径 —— 它们不该被标 fail closed"
    )
    assert "正常" in rows["B"], "B 那一行必须说明它是正常类别而不是缺口"

    assert "30.6%" in gap and "1.2%" in gap, (
        "必须把「B 是正常类别」与「真正的缺口 1.2%」分开，否则 31.7% 会被读成 31.7% 有问题"
    )
    assert "未重测" in gap, "引用了生产库实测却没有标明本票未重测"


def test_the_deviation_is_recorded_and_points_at_the_batch_that_fixes_it() -> None:
    l4 = _section("L4")
    dev = _tail(l4, "L4-3").split("\n### ")[0]
    assert "绕开" in dev, "偏离那条没点明是「绕开」而不是「缺少授权」"
    assert "不在本票解决" in dev or "本票只把它写清楚" in dev
    # The owner has to sit with the pending item, and it has to be the *right*
    # owner: D batch finished on 2026-09-30 and only changed the inbound hop, so
    # pointing the pending item at it means nothing picks it up.
    # `待收敛` also appears in the heading, so anchor on the sentence that
    # *records* the pending item, not the first occurrence of the word.
    i = dev.index("记为待收敛项")
    block = dev[i : i + 200]  # the pending sentence and what immediately follows
    assert "后续票" in block, "「待收敛项」没写由谁承接 —— 承接方必须写在同一个句子或紧随的一句里"
    assert "不要把它挂到已完成的 D 批" in block, (
        "没点明**不要**把它挂到已完成的 D 批 —— 那正是「记了却没人接」的形态"
    )


def test_it_does_not_let_shop_scope_stand_in_for_partner_authorization() -> None:
    """`@ShopDataScope(column="site_id")` filters by *shop*, not by 代理商.

    The chain says "the backend already implements this", which is true — but
    the thing it implements compares the caller's shop set against the order's
    site. Reading that as "代理商 authorization" is a different claim, and the
    two disagree whenever a site's shop binding and its partner differ.
    """
    l4 = _section("L4")
    chain = _tail(l4, "L4-1").split("\n### ")[0]
    assert "它不核 `partner_b_id`" in chain or "不核对 `partner_b_id`" in chain, (
        "没写清这套现成隔离**不核 `partner_b_id`**"
    )
    assert "店铺集合" in chain and "代理商" in chain
    assert "不一致" in chain, "没写「店铺绑定与代理商归属不一致时两者会给出不同的行集合」"


def test_it_states_what_it_did_not_do() -> None:
    l4 = _section("L4")
    not_done = _tail(l4, "L4-5").split("\n### ")[0]
    assert "不实现" in not_done, "没写明本票不实现订单授权"
    assert "不重测" in not_done, "没写明数字来自既有实测、本票未重测"
    # Every "not done" item that has an owner must name a live one. This line
    # pointed at D batch, which finished on 2026-09-30.
    assert "不是 D 批" in not_done or "不是已完成的 D 批" in not_done, (
        "「不定两条路选哪条」那一项又指向了已完成的 D 批 —— 承接方必须是后续票"
    )


def test_the_delivery_records_agree_on_who_carries_the_pending_item() -> None:
    """`validation.md` and `开发进度.md` must not point back at D batch.

    The correction landed in the baseline first; these two were the places a
    reader goes for "what happens next", and they still said D batch.
    """
    for name in ("validation.md", "开发进度.md"):
        text = (PROJECT_ROOT / "docs" / name).read_text(encoding="utf-8")
        # Only the #479 milestone block matters here.
        if "#479" not in text:
            pytest.skip(f"{name} 里没有 #479 条目")
        start = text.index("#479")
        # End at the *next* milestone heading: a fixed-width window reaches into
        # whatever entry follows, so a later milestone could satisfy these
        # checks while #479 itself lost them.
        rest = text[start:]
        nxt = re.search(r"\n## ", rest)
        block = rest[: nxt.start()] if nxt else rest
        assert "后续票" in block, f"{name} 的 #479 条目没写清待收敛项由谁承接"
        assert "指向 D 批" not in block and "留给 D 批" not in block, (
            f"{name} 的 #479 条目仍把待收敛项指向已完成的 D 批"
        )
        assert "未完成业务验收" in block, (
            f"{name} 的 #479 条目没写明「未完成业务验收」—— AGENTS.md 要求这一状态必须写出来"
        )


def test_it_records_where_the_baseline_fell_short() -> None:
    """The section's own rule: a gap gets backfilled, not routed around."""
    l4 = _section("L4")
    backfill = _tail(l4, "L4-4").split("\n### ")[0]
    assert "回填" in backfill
    # The rows must name *where each gap went*, not just mention the layers
    # somewhere: a backfill record whose target column says "一处" records
    # nothing a reader can follow.
    rows = [r for r in backfill.splitlines() if r.startswith("|") and r.count("|") >= 4]
    rows = [r for r in rows if not set(r.replace("|", "").strip()) <= set("-: ")]
    assert len(rows) >= 2, f"回填记录只有 {len(rows)} 行数据"
    targets = {c.strip() for row in rows for c in row.strip("|").split("|")}
    assert "**L3-1**" in targets and "**L3-3**" in targets, (
        f"回填记录没写清撞到的是 L3 的哪两处，目标列实际有：{sorted(targets)}"
    )


def test_no_company_source_body_is_pasted() -> None:
    assert "```" not in _section("L4"), "L4 里出现了代码块 —— 基线不搬运公司源码正文"


@pytest.mark.skipif(
    not (os.environ.get("AIOPS_GL_CA") and Path(os.environ["AIOPS_GL_CA"]).exists()),
    reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核",
)
def test_the_two_endpoints_of_the_chain_still_say_what_the_chain_assumes() -> None:
    """Order -> site and site -> partner are the chain's two load-bearing edges."""
    po = "cloud-charging-pile-data/src/main/java/com/qushiyun/cloud/charging/pile/data/po"
    mapper = "cloud-charging-pile-data/src/main/java/com/qushiyun/cloud/charging/pile/data/mapper"
    cases = [
        ("363", "release", f"{po}/ChOrderInfo.java", "private String siteId;"),
        ("363", "release", f"{po}/ChSite.java", "private String partnerBId;"),
        # The isolation annotation the chain's step 4 rests on.
        ("363", "release", f"{mapper}/ChOrderInfoMapper.java", '@ShopDataScope(column = "site_id"'),
    ]
    for project, ref, path, marker in cases:
        proc = subprocess.run(
            ["bash", SCRIPT, "raw", "--project", project, "--ref", ref, "--path", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"{path}\n{proc.stderr}"
        assert marker in proc.stdout, f"{path} 里找不到 {marker}"
