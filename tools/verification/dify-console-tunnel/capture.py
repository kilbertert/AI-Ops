"""用真实登录会话打开 Dify 控制台，截取「复制并开通客户 AI 服务」面板。

需要先起隧道与代理（见本目录 README）。凭据只从库外 0600 会话文件读，不打印。

    uv run python tools/verification/dify-console-tunnel/capture.py --out /tmp/panel.png
    uv run python tools/verification/dify-console-tunnel/capture.py --out /tmp/panel.png --expand
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_STATE = Path("~/.cache/aiops-dify-self-service-research/real-acceptance/dify-session.json")


async def run(args) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright is not installed (uv add --dev playwright)", file=sys.stderr)
        return 2
    state = Path(args.state).expanduser()
    if not state.is_file():
        print(f"session file not found: {state}", file=sys.stderr)
        return 2
    cookies = [
        # `__Host-` cookie 不能带 domain，必须按 url 设；带 domain 会被整条丢弃，
        # 页面退回登录页，看起来像会话过期。
        {"name": c["name"], "value": c["value"], "url": args.base}
        for c in json.loads(state.read_text())["cookies"]
    ]
    async with async_playwright() as p:
        browser = await p.chromium.launch(proxy={"server": args.proxy})
        context = await browser.new_context(viewport={"width": 1400, "height": 1600})
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(args.base + "/apps", wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(7000)
        if args.expand:
            await page.get_by_role("button", name="复制并开通客户 AI 服务").click()
            await page.wait_for_timeout(2500)
        await page.screenshot(path=args.out, full_page=True)
        body = await page.inner_text("body")
        print(f"screenshot: {args.out}")
        print("logged in:", "请用公司账号登录" not in body)
        if args.grep:
            print(f"{args.grep!r} in page:", args.grep in body)
        await browser.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--base", default="https://dify.ranlei.work")
    parser.add_argument("--proxy", default="http://127.0.0.1:18080")
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--expand", action="store_true", help="展开「复制并开通客户 AI 服务」面板")
    parser.add_argument("--grep", default="", help="在页面正文里查一个串，打印是否存在")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
