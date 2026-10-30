"""知识库页的**静态形状**判据（#926 的部署层）。

这一页是静态资产、没有前端构建，所以没有任何单测会执行它 —— 而 2026-10-22 的真机
验收正好在它身上翻了一次车：`/v1/knowledge/available` 返回 200、数据齐全，页面上却
**一个复选框都没有**，因为 `renderAvailable` 把每个 `label` 造好了却没 append 进容器。
接口层全绿，故障只在浏览器里。

所以这里判三件事：

1. 渲染函数**把行塞进容器**（就是上面那个缺陷）；
2. 页面不把令牌写进除 `localStorage` 以外的地方；
3. 页面发起的请求带 `X-Business-Entry: operator`（公网 nginx 只有这条分支保留调用者的
   Authorization，consumer 分支会换成服务令牌 —— 少了它，令牌根本到不了门）。

判的是形状，不是渲染结果。真正的渲染验收在 `docs/validation.md` 的浏览器记录里。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
PAGE = ROOT / "src" / "aiops_diagnostics" / "knowledge" / "static" / "knowledge.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_each_rendered_row_is_attached_to_its_container() -> None:
    """`renderAvailable` 造完行必须 `append` 进容器。

    只断言"函数里出现了 append"不够：要钉住它 append 的**是那个容器**。
    2026-10-22 的缺陷正是"造好了行，但没有任何一处把它放进去"。
    """
    page = _page()
    start = page.index("function renderAvailable")
    end = page.index("function renderSources", start)
    body = page[start:end]

    assert "box.append(row)" in body, (
        "renderAvailable 没有把行塞进容器 —— 接口有数据时页面会是空白列表，"
        "而且没有任何报错（2026-10-22 真机缺陷）"
    )


def test_the_page_forwards_the_caller_authorization_to_the_operator_entry() -> None:
    page = _page()
    assert '"X-Business-Entry": "operator"' in page


def test_the_page_keeps_the_token_only_in_localstorage() -> None:
    """令牌不落 cookie、不落 URL、不拼进 DOM 文本。"""
    page = _page()
    assert "localStorage.setItem(TOKEN_KEY" in page
    assert "document.cookie" not in page
    assert "?token=" not in page and "&token=" not in page
    # 不把令牌写进 innerHTML/textContent（那会让它出现在页面与被复制的内容里）
    assert not re.search(r"(innerHTML|textContent)\s*=\s*[^;]*token\(\)", page)
