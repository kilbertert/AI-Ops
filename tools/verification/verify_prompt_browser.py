"""在隔离Dify实际编辑SYSTEM，再经真实发布桥预览；仅网络地址转发。"""

import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def run(root, namespace, operator=False):
    prefix = "operator" if operator else "prompt"
    ready = json.loads((root / (prefix + "-browser-ready.json")).read_text())
    marker = "管家提示词页面核对" if operator else "提示词页面保存核对"
    order = "ORDER-1001" if operator else "ORDER-3001"

    def address(port):
        return (
            "http://" + subprocess.check_output(["docker", "port", namespace, str(port)], text=True).strip()
        )

    web, api, bridge = (address(p) for p in (3000, 5001, 5004))
    saved, responses, formal = [], [], []

    def formal_answer():
        script = """
import json,time,urllib.request,sys
base=sys.argv[1]
operator=sys.argv[2]=='operator'
headers={'Authorization':'Bearer '+('operator-private' if operator else 'dify-chat-real-token'),
         'X-Business-Entry':'operator' if operator else 'consumer',
         'Content-Type':'application/json'}
body=({'question':'请核对本轮订单事实','order_no':'ORDER-1001'} if operator
      else {'question':'我上一个订单现在是什么情况'})
req=urllib.request.Request(base+'/v1/assistant/questions',data=json.dumps(body).encode(),headers=headers)
with urllib.request.urlopen(req,timeout=20) as r: qa=json.load(r)['qa_id']
until=time.monotonic()+95
while time.monotonic()<until:
 req=urllib.request.Request(base+'/v1/assistant/questions/'+qa,headers=headers)
 with urllib.request.urlopen(req,timeout=20) as r: result=json.load(r)
 if result.get('status') not in ('queued','running'):
  assert result['status']=='completed',result
  print(json.dumps(result['result'],ensure_ascii=False));break
 time.sleep(.2)
else: raise TimeoutError('formal answer')
"""
        answer = subprocess.check_output(
            [
                "docker",
                "exec",
                namespace,
                "/home/claude/miniconda3/bin/python",
                "-c",
                script,
                ready["gateway"],
                "operator" if operator else "consumer",
            ],
            text=True,
            timeout=120,
        )
        formal.append(json.loads(answer))
        return answer

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-CN", viewport={"width": 1600, "height": 1100})
        context.add_cookies(
            [
                {"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                for item in ready["headers"]["Cookie"].split("; ")
                for k, v in [item.split("=", 1)]
            ]
        )

        def forward_api(route):
            response = route.fetch(
                url=route.request.url.replace("http://127.0.0.1:5001", api, 1),
                headers={**route.request.headers, **ready["headers"]},
                timeout=90000,
            )
            route.fulfill(response=response)

        def forward_bridge(route):
            response = route.fetch(url=route.request.url.replace(web, bridge, 1), timeout=90000)
            route.fulfill(response=response)

        context.route("http://127.0.0.1:5001/**", forward_api)
        context.route(web + "/__aiops/**", forward_bridge)
        page = context.new_page()

        def recorded(response):
            if response.request.method == "POST" and response.url.endswith("/workflows/draft"):
                body = response.request.post_data_json
                node = next(n["data"] for n in body["graph"]["nodes"] if n["id"] == "answer")
                saved.append({"status": response.status, "prompts": node.get("prompt_template")})
            if response.request.method == "POST" and response.url.endswith("/model-config"):
                saved.append(
                    {
                        "status": response.status,
                        "prompts": [{"text": response.request.post_data_json.get("pre_prompt", "")}],
                    }
                )
            if "/__aiops/" in response.url:
                if "operator-preview" in response.url:
                    responses.append({"path": "operator-preview", "status": response.status})
                    return
                responses.append(
                    {
                        "path": response.url.split("/__aiops/")[1],
                        "status": response.status,
                        "body": response.json(),
                    }
                )

        page.on("response", recorded)
        try:
            page.goto(
                web + "/app/" + ready["app_id"] + ("/configuration" if operator else "/workflow"),
                wait_until="domcontentloaded",
            )
            if not operator:
                page.locator('.react-flow__node[data-id="answer"]').wait_for(timeout=60000)
                page.mouse.click(1567, 86)
                page.mouse.move(750, 820)
                page.keyboard.down("Space")
                page.mouse.down()
                page.mouse.move(350, 820, steps=12)
                page.mouse.up()
                page.keyboard.up("Space")
                page.get_by_role("button", name="选择答复类型与知识引用", exact=True).click()
            editors = page.locator('[contenteditable="true"]')
            editors.first.wait_for()
            (root / (prefix + "-editors.json")).write_text(
                json.dumps(editors.all_text_contents(), ensure_ascii=False)
            )
            editor = editors.first if operator else editors.filter(has_text="充电").first
            changed = "回答简洁直接，先给结论。验收标记：" + marker + "。"
            editor.click()
            page.keyboard.press("Control+End")
            page.keyboard.press("Enter")
            page.keyboard.insert_text(changed)
            page.keyboard.press("Tab")
            if operator:
                page.get_by_role("button", name="重新开始", exact=True).last.click()
                chat = page.get_by_role("textbox").last
                chat.fill("订单号: " + order)
                chat.press("Enter")
                page.wait_for_function(
                    "marker => document.body.innerText.includes(marker + '。\\n订单号')",
                    arg=marker,
                    timeout=120000,
                )
                assert marker not in formal_answer(), "草稿提前影响正式版本"
                with page.expect_response(
                    lambda r: (
                        r.url.endswith("/__aiops/manage")
                        and r.request.post_data_json.get("action") == "publish"
                    )
                ) as publication:
                    page.get_by_role("button", name="发布到管家", exact=True).click()
                assert publication.value.status == 200
                assert marker in formal_answer(), "发布后没有采用管家提示词"
                page.get_by_text("版本操作", exact=True).click()
                page.get_by_role("button", name="查看历史", exact=True).click()
                page.get_by_label("历史版本").select_option(str(ready["version_no"]))
                with page.expect_response(
                    lambda r: (
                        r.url.endswith("/__aiops/manage")
                        and r.request.post_data_json.get("action") == "version"
                    )
                ) as restored:
                    page.get_by_role("button", name="恢复所选版本", exact=True).click()
                assert restored.value.status == 200
                assert marker not in formal_answer(), "恢复未采用旧管家提示词"
                return {
                    "passed": True,
                    "scope": "operator page, Dify, bridge, Gateway, Java; synthetic identity, data and model",
                }
            until = time.monotonic() + 20
            while not saved and time.monotonic() < until:
                page.wait_for_timeout(200)
            assert any(
                s["status"] == 200 and any(changed in t.get("text", "") for t in s["prompts"]) for s in saved
            ), "SYSTEM编辑未保存"
            page.add_script_tag(path=str(ROOT / "ops/dify-console-bridge/dify-publish-button.js"))
            page.once("dialog", lambda dialog: dialog.accept("订单号: " + order))
            page.locator("#aiops-trial-btn").click(timeout=15000)
            until = time.monotonic() + 120
            while not any(r["path"] == "trial/result" and r["body"].get("terminal") for r in responses):
                assert time.monotonic() < until, "试运行未进入终态"
                page.wait_for_timeout(200)
            terminal = next(r["body"] for r in reversed(responses) if r["path"] == "trial/result")
            assert terminal.get("status") == "completed", terminal
            text = page.locator("#aiops-trial-panel").inner_text()
            assert order in text and marker in text, text
            page.screenshot(path=str(root / (prefix + "-preview.png")), full_page=True)
            assert marker not in formal_answer(), "草稿提前影响正式版本"
            with page.expect_response(lambda r: r.url.endswith("/__aiops/publish")) as publication:
                page.locator("#aiops-publish-btn").click()
            assert publication.value.json()["published"] is True
            assert marker in formal_answer(), "发布后正式入口没有采用页面提示词"
            page.once("dialog", lambda dialog: dialog.accept(str(ready["version_no"])))
            with page.expect_response(lambda r: r.url.endswith("/__aiops/restore")) as restoration:
                page.locator("#aiops-restore-btn").click()
            assert restoration.value.json()["published"] is True
            assert marker not in formal_answer(), "恢复后未回到原提示词"
            return {"passed": True, "scope": prefix + " editor, preview, publish, restore; synthetic model"}
        finally:
            (root / (prefix + "-browser-saves.json")).write_text(json.dumps(saved, ensure_ascii=False))
            (root / (prefix + "-formal-results.json")).write_text(json.dumps(formal, ensure_ascii=False))
            (root / (prefix + "-browser-responses.json")).write_text(
                json.dumps(responses, ensure_ascii=False)
            )
            (root / (prefix + "-browser-text.txt")).write_text(page.locator("body").inner_text())
            page.screenshot(path=str(root / (prefix + "-browser-final.png")), full_page=True)
            browser.close()


if __name__ == "__main__":
    root = Path(sys.argv[1])
    operator = "--operator" in sys.argv[3:]
    try:
        result = run(root, sys.argv[2], operator)
    except Exception as exc:
        result = {"passed": False, "error": str(exc).split("Call log:")[0]}
    (root / (("operator" if operator else "prompt") + "-browser-result.json")).write_text(
        json.dumps(result, ensure_ascii=False)
    )
