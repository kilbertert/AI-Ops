"""知识库入口的 nginx 接线（#985 / PRD #981）。

三处改动各自都能"看着配了其实没生效"，所以逐处钉住：

1. **脚本必须真的被注入** —— `sub_filter` 换的是 `</body>`，少一个字符就静默不注入，
   而页面上看不出任何异常（发布按钮也有同样的前提）。
2. **脚本必须从我们这里出** —— `alias` 指错就是一个 404 的 `<script>`，浏览器把它当
   空脚本执行，页面照常，入口永远不出现。
3. **转发必须把会话带下去** —— 少了 `Cookie`，桥解析不出身份，运营看到的是"请先登录"，
   而他明明登录着。

第 3 条尤其容易写成"照抄发布那条"：那条把 `Content-Type` 定死成 JSON、body 限 4k，
而知识库入口是浏览器的顶层 GET —— 照抄会让它在浏览器里 4xx。
"""

from __future__ import annotations

import re
from pathlib import Path

OPS = Path(__file__).parents[1] / "ops" / "dify-console-bridge"


def _console() -> str:
    return (OPS / "dify-console-10008.conf").read_text(encoding="utf-8")


def _snippets() -> str:
    return (OPS / "dify-console-publish.conf").read_text(encoding="utf-8")


def test_the_script_is_injected_next_to_the_publish_button() -> None:
    """注入行里必须同时有两个 `<script>`，且都换在 `</body>` 上。"""
    text = _console()
    injected = [line for line in text.splitlines() if "sub_filter '</body>'" in line]
    assert len(injected) == 1, "应当只有一条 `</body>` 注入行"
    line = injected[0]
    assert "/__aiops/publish-button.js" in line
    assert "/__aiops/dify-knowledge-open.js" in line
    assert line.rstrip().endswith("</body>';")


def test_the_script_is_served_from_our_own_path() -> None:
    text = _console()
    block = re.search(r"location = /__aiops/dify-knowledge-open\.js \{(.*?)\}", text, flags=re.S)
    assert block, "缺少 /__aiops/dify-knowledge-open.js 的 alias"
    body = block.group(1)
    assert "alias /srv/aiops-console-assets/dify-knowledge-open.js" in body
    assert "application/javascript" in body
    assert "no-store" in body


def test_the_console_forwards_the_knowledge_route_to_the_bridge() -> None:
    """10008 要 include 那个 snippets 文件，而它必须真的含这条 location。"""
    console = _console()
    assert "include /www/server/panel/vhost/nginx/snippets/dify-console-publish.conf;" in console
    assert "location = /__aiops/knowledge-open {" in _snippets()


def test_the_forwarding_carries_the_session_and_does_not_copy_the_publish_gates() -> None:
    """两条判据，方向相反：

    * **必须**把 `Cookie` 转下去（否则桥认不出身份）；
    * **不得**照抄发布那条的 JSON Content-Type / 4k body 限制 —— 知识库入口是浏览器的
      顶层 GET，照抄会让它在浏览器里 4xx，而发布那条"缺了就拒"是对的。
    """
    text = _snippets()
    block = re.search(r"location = /__aiops/knowledge-open \{(.*?)\n\}", text, flags=re.S)
    assert block, "缺少 /__aiops/knowledge-open 的转发"
    body = block.group(1)
    assert "proxy_set_header Cookie $http_cookie;" in body
    assert "proxy_pass http://127.0.0.1:10118;" in body
    assert "Content-Type" not in body, "这条不改请求体，不该定 Content-Type"
    assert "client_max_body_size" not in body, "顶层 GET 没有 body，不该照抄 4k 限制"
    assert "X-CSRF-Token" not in body, "顶层导航不携带 CSRF，转一个空头只会误导下游"


def test_the_forwarding_does_not_pass_url_arguments() -> None:
    """**不转发 `$arg_*`** —— 租户只来自会话。转发 URL 参数等于给"按 URL 指定租户"
    留了一条可以加回去的口子（#979 那个洞的形状）。"""
    text = _snippets()
    block = re.search(r"location = /__aiops/knowledge-open \{(.*?)\n\}", text, flags=re.S)
    assert block
    assert "$arg_" not in block.group(1)
    assert "$query_string" not in block.group(1)
