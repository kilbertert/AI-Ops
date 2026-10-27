"""在宿主浏览器操作隔离Dify真实节点；转发仅改端口，响应来自真实API。"""

import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def run(root, namespace):
    ready = json.loads((root / "picker-browser-ready.json").read_text())

    def address(port):
        return (
            "http://" + subprocess.check_output(["docker", "port", namespace, str(port)], text=True).strip()
        )

    web, api = address(3000), address(5001)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1050})
        cookies = [
            {"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
            for item in ready["headers"]["Cookie"].split("; ")
            for k, v in [item.split("=", 1)]
        ]
        context.add_cookies(cookies)

        def forward(route):
            try:
                response = route.fetch(
                    url=route.request.url.replace("http://127.0.0.1:5001", api, 1),
                    headers={**route.request.headers, **ready["headers"]},
                    timeout=90000,
                )
                route.fulfill(response=response)
            except Exception:
                route.abort()

        context.route("http://127.0.0.1:5001/**", forward)
        page = context.new_page()
        saved = []

        def recorded(response):
            if response.request.method == "POST" and response.url.endswith("/workflows/draft"):
                body = response.request.post_data_json
                node = next((n["data"] for n in body["graph"]["nodes"] if n["id"] == "knowledge"), {})
                saved.append({"status": response.status, "config": node.get("tool_configurations")})

        page.on("response", recorded)
        try:
            for _ in range(30):
                try:
                    page.goto(web + "/app/" + ready["app_id"] + "/workflow", wait_until="domcontentloaded")
                    break
                except Exception:
                    time.sleep(1)
            node = page.locator('.react-flow__node[data-id="knowledge"]')
            node.wait_for(timeout=60000)
            node.click()
            page.get_by_text("FAQ A", exact=True).last.click()
            page.get_by_text("FAQ B", exact=True).last.click()
            page.get_by_text("FAQ A", exact=True).last.click()
            page.keyboard.press("Escape")
            until = time.monotonic() + 30
            expected = {"knowledge_base_ids": {"type": "constant", "value": ["kb-b"]}}
            while not any(s == {"status": 200, "config": expected} for s in saved):
                if time.monotonic() >= until:
                    raise AssertionError("浏览器修改未收到草稿保存成功响应")
                page.wait_for_timeout(200)
            page.screenshot(path=str(root / "picker-browser.png"), full_page=True)
            return {"passed": True}
        finally:
            (root / "picker-browser-saves.json").write_text(json.dumps(saved))
            (root / "picker-browser-text.txt").write_text(page.locator("body").inner_text())
            page.screenshot(path=str(root / "picker-browser-final.png"), full_page=True)
            browser.close()


if __name__ == "__main__":
    root = Path(sys.argv[1])
    try:
        result = run(root, sys.argv[2])
    except Exception as exc:
        result = {"passed": False, "error": str(exc).split("Call log:")[0]}
    (root / "picker-browser-result.json").write_text(json.dumps(result))
