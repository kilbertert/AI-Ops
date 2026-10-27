"""ADR-0097 的归档冻结必须**自动**生效，而不是靠人记得。

ADR-0097 把 `docs/validation.md` 与 `docs/开发进度.md` 冻结为只读归档，新记录一律
`docs/validation/<日期>-<短名>.md`。决定之后仍然有两次提交直接往归档文件追加了整节
（#1014 / #1015）—— 因为当时**没有任何东西会失败**，只有事后人工比对才发现。

这里的判据刻意不比对文件哈希：归档文档允许被订正（错别字、失效链接改指向）。
它守的是那条真正会再犯的规则 —— **归档里不能出现冻结日期之后的新记录标题**。

## 为什么按标题判而不是按路径判

一次追加的形态是「在文件顶部插入一个 `## <日期> · <标题>`」。只要那个日期晚于冻结日，
无论作者写在哪一节、用了什么措辞，都是新记录。反过来，订正一个旧标题里的错别字不会
让测试转红，因为日期没变。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: ADR-0097 采纳日。归档里只允许出现**这一天及更早**的记录标题。
FREEZE_DATE = "2026-10-23"

ARCHIVES = ("docs/validation.md", "docs/开发进度.md")

HEADING = re.compile(r"^##\s+(20\d{2}-\d{2}-\d{2})", re.MULTILINE)


def _headings(path: Path) -> list[str]:
    return HEADING.findall(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", ARCHIVES)
def test_the_archive_still_says_it_is_frozen(relative: str) -> None:
    """文件头必须留着冻结声明，否则后来的人看不出它只读。"""
    text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:8])
    assert "只读归档" in head, f"{relative} 顶部缺少「只读归档」声明"
    assert "ADR-0097" in head, f"{relative} 顶部没有指向 ADR-0097"


@pytest.mark.parametrize("relative", ARCHIVES)
def test_no_record_newer_than_the_freeze_date(relative: str) -> None:
    """冻结日之后的新记录必须写进 `docs/<目录>/<日期>-<短名>.md`，不能追加到归档。

    转红时的修法：把那一节整段移到按日期的新文件里，归档里删掉。**不要**改
    这里的 FREEZE_DATE 来让它变绿 —— 那等于取消这条规则。
    """
    dates = _headings(PROJECT_ROOT / relative)
    assert dates, f"{relative} 里一个日期标题都没有，判据本身失效了"
    too_new = sorted({d for d in dates if d > FREEZE_DATE})
    assert not too_new, (
        f"{relative} 里出现了 {FREEZE_DATE} 之后的记录标题：{too_new}。"
        f"ADR-0097 要求新记录写成 docs/<目录>/<日期>-<短名>.md，不要追加到归档文件。"
    )
