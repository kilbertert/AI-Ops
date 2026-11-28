"""知识库入口与退役 Nginx sub_filter 注入的接线判据（#985 / PRD #981 / ADR-0366）。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
OPS = ROOT / "ops" / "dify-console-bridge"
WEB_PATCHES = ROOT / "deploy" / "dify-36" / "web-patches" / "1.17.1"


def _console() -> str:
    return (OPS / "dify-console-10008.conf").read_text(encoding="utf-8")


def _snippets() -> str:
    return (OPS / "dify-console-publish.conf").read_text(encoding="utf-8")


def test_nginx_subfilter_js_injection_is_retired() -> None:
    """确认 10008 不再通过 Nginx sub_filter 或 `/__aiops/*.js` 注入脚本（ADR-0366）。"""
    text = _console()
    active_lines = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "sub_filter" not in active_lines
    assert "/__aiops/publish-button.js" not in active_lines
    assert "/__aiops/dify-knowledge-open.js" not in active_lines


def test_knowledge_open_card_is_native_react_component_without_secrets() -> None:
    """RAGFlow 入口已迁入正式 React 组件 `aiops-knowledge-open.tsx` 并挂载到根布局。"""
    src = (WEB_PATCHES / "aiops-knowledge-open.tsx").read_text(encoding="utf-8")
    assert "pathname === '/datasets' || pathname.startsWith('/datasets/')" in src
    assert "localStorage" not in src
    assert "setInterval" not in src
    assert "'/__aiops/knowledge-open'" in src
    assert "BUILD_KEY" not in src and "TRIAL_KEY" not in src

    patch_txt = (WEB_PATCHES / "18-liquid-glass-theme.patch").read_text(encoding="utf-8")
    assert "AiopsKnowledgeOpenCard" in patch_txt
    assert "+        <AiopsKnowledgeOpenCard />" in patch_txt


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
