"""ADR-0097 的归档冻结必须**自动**生效，而不是靠人记得。

ADR-0097 把 `docs/validation.md` 与 `docs/开发进度.md` 冻结为只读归档，新记录一律
`docs/validation/<日期>-<短名>.md`。决定之后仍然有两次提交直接往归档文件追加了整节
（#1014 / #1015）—— 因为当时**没有任何东西会失败**，只有事后人工比对才发现。

## 为什么按「标题集合」判，而不是按日期判

第一版判据写的是「归档里不许出现晚于冻结日的标题」。它有洞，而且洞正好盖住了它要防的那次
违规：#1015 的标题就是冻结当天（2026-10-23）的日期，`>` 判据放它过去；把条件改成 `>=`
又会把归档里**本来就有的**那批 10-23 旧记录全部判红。

所以判据改成**基线集合**：`milestone_archive_headings.json` 记下冻结时两份归档的全部标题。

- 出现基线里没有的标题 ⇒ **红**（无论日期是哪天，包括冻结当天）
- 基线里有、现在没有的标题 ⇒ 允许（订正、合并、改措辞是归档的正当维护）

这份基线只在**有意重写归档**时才更新，并要在对应 PR 里说明理由。为了让守卫变绿而往基线里
塞一个新标题，等于取消这条规则。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "tests/milestone_archive_headings.json"

#: 记录标题的形态是 `## <日期> · <标题>`；只取整行，日期与正文都算身份的一部分。
HEADING = re.compile(r"^##\s+20\d{2}-\d{2}-\d{2}[^\n]*", re.MULTILINE)


def _headings(relative: str) -> list[str]:
    return HEADING.findall((PROJECT_ROOT / relative).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", ("docs/validation.md", "docs/开发进度.md"))
def test_the_archive_still_says_it_is_frozen(relative: str) -> None:
    """文件头必须留着冻结声明，否则后来的人看不出它只读。"""
    head = "\n".join((PROJECT_ROOT / relative).read_text(encoding="utf-8").splitlines()[:4])
    assert "只读归档" in head, f"{relative} 顶部缺少「只读归档」声明"
    assert "ADR-0097" in head, f"{relative} 顶部没有指向 ADR-0097"


@pytest.mark.parametrize("relative", ("docs/validation.md", "docs/开发进度.md"))
def test_no_heading_outside_the_frozen_baseline(relative: str, baseline: dict) -> None:
    """归档只许少、不许多 —— 新增任何一节（含冻结当天的日期）都必须写进按日期的新文件。

    转红时的修法：把那一节整段移到 `docs/<目录>/<日期>-<短名>.md`，归档里删掉。
    **不要**把这个标题加进 `tests/milestone_archive_headings.json` 来让它变绿。
    """
    frozen = set(baseline["archives"][relative])
    current = _headings(relative)

    assert current, f"{relative} 里一个记录标题都没有，判据本身失效了"

    added = [h for h in current if h not in frozen]
    assert not added, (
        f"{relative} 新增了 {len(added)} 个不在冻结基线里的记录标题：\n  "
        + "\n  ".join(added[:5])
        + f"\nADR-0097 要求新记录写成 docs/<目录>/<日期>-<短名>.md（冻结日 {baseline['freeze_date']} "
        "之后的算新记录，当天的追加也算）。不要改基线文件来绕过。"
    )


def test_the_baseline_covers_both_archives(baseline: dict) -> None:
    """基线本身要完整，否则缺的那一份等于没有守卫。"""
    assert set(baseline["archives"]) == {"docs/validation.md", "docs/开发进度.md"}
    for relative, headings in baseline["archives"].items():
        assert len(headings) > 100, f"{relative} 的基线只有 {len(headings)} 条，像是不完整"
        assert all(h.startswith("## ") for h in headings)
