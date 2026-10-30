#!/usr/bin/env python3
"""用**与现役产物同构的合成 DOM** 校准助手区域选择器（不碰生产）。

为什么需要它：选择器是靠类名命中的，而类名来自公司构建产物。猜错时
`status_probe.py` 会"取不到区域 ⇒ 判失败"，不会误报通过 —— 但也从来没命中过。
这个脚本用一段本地 HTML 复现现役产物的渲染分支，验证选择器与用户气泡的边界。

**它不加载公司产物、不发任何网络请求到生产**：页面用 `set_content` 注入，
所有 `/v1/` 请求都被 route 拦截并返回合成数据。

用法：
    uv run --with playwright python tools/verification/consumer-status/replay_selector.py

退出码：0 选择器符合预期 / 1 不符合
"""

from __future__ import annotations

import json
import sys

ORDER = "SYNTH-ORDER-0001"
ANSWER = (
    f"Order {ORDER}: finished (status code 1).\n"
    "Source: cpms.chargingInfo; observed at (UTC): 2026-10-23T03:21:45+00:00."
)

#: 与 41 上 `static/js/aiPackage-pages-chat-chat.d86b6f8a.js` 的渲染分支同构：
#:   role === "user" ? .user-bubble : .ai-row > .ai-msg > .bubble-text
DOM = f"""
<html><body><div id="app">
  <div class="user-bubble"><span class="bubble-text">check my latest order status</span></div>
  <div class="ai-row"><div class="ai-msg">
    <span class="bubble-text">{ANSWER}</span>
  </div></div>
</div></body></html>
"""

#: 曾经用过的猜测选择器 —— 保留在这里，是为了让"它命中 0"这件事可回归。
LEGACY_GUESSES = (".msg-list .msg-item .content", ".chat-list .chat-item .content")


def _run() -> int:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("sp", Path(__file__).with_name("status_probe.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    from playwright.sync_api import sync_playwright

    out: dict[str, object] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_content(DOM)
        page.wait_for_timeout(150)

        hits = {}
        for selector in module.ASSISTANT_SELECTORS:
            loc = page.locator(selector)
            count = loc.count()
            hits[selector] = {
                "count": count,
                "has_order": bool(count) and ORDER in loc.last.inner_text(),
            }
        out["current"] = hits
        out["legacy_guess_counts"] = {s: page.locator(s).count() for s in LEGACY_GUESSES}
        user = page.locator(".user-bubble")
        out["user_bubble"] = {
            "count": user.count(),
            "has_order": bool(user.count()) and ORDER in user.inner_text(),
        }
        browser.close()

    ok = (
        any(v["count"] and v["has_order"] for v in hits.values())
        and all(c == 0 for c in out["legacy_guess_counts"].values())
        and out["user_bubble"]["count"] == 1
        and out["user_bubble"]["has_order"] is False
    )
    out["verdict"] = "pass" if ok else "fail"
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run())
