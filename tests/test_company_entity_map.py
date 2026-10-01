"""The L2 entity map cites its sources, and keeps the two things apart (#477).

L2's whole claim is that every edge has a `仓:分支:文件:行` source — the layer
says so in its own header, and "no source means no conclusion, only a to-do".
That claim is what this file checks, because the failure mode is quiet: an
edge added without a source reads exactly like one that has it.

Two further properties are structural rather than stylistic, and both come
from a mistake this layer is specifically prone to:

* **"平台" is a sentinel, not a table.** It appears in the entity *table* of
  the document only in the row that says so; if it ever gains a `@TableName`,
  the map has started inventing an entity.
* **`shop_id` ≠ `site_id`.** The near-equality is recorded with its magnitude;
  dropping that note is how a 千分之一 difference becomes "interchangeable".

The live half is skipped without an identity anchor, like the other two
evidence tests.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "docs" / "agents" / "company-code-baseline.md"
GENERATOR = PROJECT_ROOT / "tools" / "company_repo_index.py"
SCRIPT = "deploy/company-gitlab-api.sh"

#: `仓:分支:文件:行` — a path with a line number, the only shape L2 accepts.
_CITATION = re.compile(r"`[A-Za-z0-9_./-]+:(?:[A-Za-z0-9_./-]+):[^`]*\.(?:java|xml|sql):\d+(?:-\d+)?`")


def _section(name: str) -> str:
    text = BASELINE.read_text(encoding="utf-8")
    start = text.index(f"## {name} ·")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _between(part: str, start: str, end: str) -> str:
    """Slice between two `### ` headings.

    Splitting on the bare label is not enough: the tables *reference* other
    parts ("见 L2-4"), so the first occurrence of a label is usually a
    cross-reference, not the heading.
    """
    return part.split(f"### {start} ")[1].split(f"### {end} ")[0]


def _table_rows(section: str) -> list[list[str]]:
    """Markdown table rows, split on unescaped pipes, header separators dropped."""
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


def test_the_entity_map_is_present_and_filled() -> None:
    l2 = _section("L2")
    assert "（尚未填充。）" not in l2, "L2 还是占位符"
    assert "L2-0" in l2 and "L2-1" in l2


def test_every_edge_row_carries_a_source() -> None:
    """L2's stated contract: no source, no conclusion.

    The edge table is the part that asserts relationships, so it is the part
    where a missing citation is a missing argument.
    """
    edge_table = _between(_section("L2"), "L2-2", "L2-3")
    rows = [r for r in _table_rows(edge_table) if len(r) >= 3 and r[0] != "边"]
    assert len(rows) >= 8, f"关键边表只解析出 {len(rows)} 行 —— 表结构可能变了"
    for row in rows:
        # A row may legitimately point at another part of L2 instead of a file
        # *only* when that part carries the file itself — so the pointer has to
        # name a heading that exists, not merely mention the layer.
        edge, semantics, source = row[0], row[1], row[2]
        if _CITATION.search(source):
            continue
        pointers = re.findall(r"L2-\d", source)
        assert pointers, f"这条边既没有出处也没有指路：{edge} / {semantics} / {source}"
        for p in pointers:
            assert f"### {p} " in _section("L2"), f"指向了不存在的节 {p}：{edge}"


def test_the_citation_shape_is_repo_branch_file_line() -> None:
    """`仓:文件:行` is not enough — the branch is part of the address.

    Checked on every citation in the layer, not only the ones in the edge
    table: a path with a line number but no branch reads as sourced and is
    not, because the line number moves between branches.
    """
    l2 = _section("L2")
    # Any `…/something.(java|xml|sql):NN` — with or without a branch prefix.
    files = re.findall(r"`([^`]*\.(?:java|xml|sql):\d+(?:-\d+)?)`", l2)
    assert len(files) >= 15, f"L2 里的文件出处太少（{len(files)} 条），可能被删了"
    for f in files:
        # `repo:branch:path/to/File.java:NN`. Split off the trailing `:NN`
        # first, then the path is everything after `repo:branch:` — a path
        # segment like `datascope/shop/…` has slashes but no colon, so counting
        # colons in `f` is what tells the two apart.
        assert f.count(":") >= 3, f"出处缺分支或行号（形如 仓:分支:文件:行）：`{f}`"

    # A citation missing its line number would not match `_CITATION` at all, so
    # the loop above cannot see it — and a row that already has one good
    # citation would still pass. Catch the omission directly: every path-looking
    # token in a code span is a citation, and every citation ends in `:NN`.
    for token in re.findall(r"`([^`]+)`", l2):
        if not re.search(r"\.(?:java|xml|sql)$", token):
            continue
        assert re.search(r":\d+(?:-\d+)?$", token), (
            f"这是一个没有行号的出处（写成 `仓:分支:文件:行`）：`{token}`"
        )


def test_platform_stays_a_sentinel_and_not_an_entity() -> None:
    """The entity table's platform row must say "no table", not name one."""
    l2 = _section("L2")
    entity_table = _between(l2, "L2-1", "L2-2")
    platform_rows = [r for r in _table_rows(entity_table) if r and "平台" in r[0]]
    assert len(platform_rows) == 1, f"实体表里「平台」出现了 {len(platform_rows)} 行"
    assert "没有表" in platform_rows[0][1], (
        "「平台」那一行不再说明它是哨兵值而不是表 —— 这正是本层最容易犯的错"
    )
    assert "哨兵" in l2, "L2 不再提「哨兵」这个词"


def test_the_near_equality_is_recorded_with_its_magnitude() -> None:
    """`shop_id` vs `site_id`: 近似 must come with 量级, not just a警告."""
    l2 = _section("L2")
    pair = _between(l2, "L2-4", "L2-5")
    assert "shop_id" in pair and "site_id" in pair
    assert re.search(r"\d+\s*行里\s*\d+\s*行不同", pair), (
        "近似对没有写明差异行数 —— 只说「不一样」而不给量级，读者无法判断是否可忽略"
    )
    assert "不要互用" in pair or "不能直接" in pair


def test_the_two_senses_of_operator_are_kept_apart() -> None:
    """The layer's own finding: 「运营商」names two different things."""
    l2 = _section("L2")
    assert "两个不同的东西" in l2 or "两个不同" in l2
    assert "partner_info" in l2
    assert "operator_id" in l2


def test_no_company_source_body_is_pasted() -> None:
    """The boundary: cite the line, do not carry the file.

    A fenced block is not automatically a violation, but L2's own text says it
    quotes nothing — so a code fence there is either a stray paste or a claim
    that needs re-wording.
    """
    l2 = _section("L2")
    assert "```" not in l2, "L2 里出现了代码块 —— 本节声明不搬运公司源码正文"


@pytest.mark.skipif(
    not (os.environ.get("AIOPS_GL_CA") and Path(os.environ["AIOPS_GL_CA"]).exists()),
    reason="未配置 AIOPS_GL_CA（身份锚点），跳过需要公司 GitLab 的复核",
)
def test_the_cited_entity_tables_still_exist_on_their_branches() -> None:
    """Re-read the two PO files L2 leans on; the table names must still match."""
    po = "cloud-charging-pile-data/src/main/java/com/qushiyun/cloud/charging/pile/data/po"
    entity = "src/main/java/com/qushiyun/cloud/mall/common/entity"
    cases = [
        ("363", "release", f"{po}/ChSite.java", "ch_site"),
        ("363", "release", f"{po}/ChOrderInfo.java", "ch_order_info"),
        ("558", "dev", f"{entity}/marketPool/PartnerInfo.java", "partner_info"),
        ("558", "dev", f"{entity}/ShopInfo.java", "shop_info"),
    ]
    for project, ref, path, table in cases:
        proc = subprocess.run(
            ["bash", SCRIPT, "raw", "--project", project, "--ref", ref, "--path", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"{path}\n{proc.stderr}"
        assert f'"{table}"' in proc.stdout, f"{path} 的 @TableName 不再是 {table}"
