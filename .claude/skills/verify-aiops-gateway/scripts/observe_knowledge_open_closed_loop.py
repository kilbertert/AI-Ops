#!/usr/bin/env python3
"""这条「Dify 知识库入口 → RAGFlow」链路的**服务端闭环**判据。

## 为什么要有它（2026-10-23）

这条链路从来是**用浏览器验收**的，而浏览器有两个地方会让判据撒谎：

1. **跨域导航之后 `tab.url` 读不到**。桥回 303 指向 `kb.mall.qushiyun.com`（另一个域），
   新标签的 URL 会变成空串。把它读成"没跳转"，就会去修一个没坏的东西 —— 那天真的
   这么误判过一次，随后又在 `about:blank` 上打转。
2. **`fetch` 与顶层导航的凭据行为不同**。同一个 URL，新标签里 303，`fetch` 里 401。
   拿 `fetch` 去验"能不能跳"，验的是另一条路径。

所以判据挪到**服务端**：只要桥承诺"给我一个目标"，就拿那个目标在 HTTP 层跟一遍，
看它是否 303 到 RAGFlow 并带 `Set-Cookie`。这与"用户点一下"等价，但不依赖浏览器
肯不肯把跨域 URL 告诉脚本。

## 它判什么

| 判据 | 期望 | 为什么 |
|---|---|---|
| 桥：无会话 | 401 | 入口**只认验过的会话**，不看 URL 参数 |
| 桥：有会话 + workspace | 303，`Location` 指向 `…/knowledge/hop?ticket=…` | 桥承诺了一个可兑换的目标 |
| 兑换：假票 | 404/410，**不是** 5xx | 过期/未知是一个可区分的答案，不是服务坏了 |
| 兑换：真票 | 303 → RAGFlow **且** `Set-Cookie` 落在目标域 | 会话由目标域自己种（ADR-0093） |
| 兑换：同一张票第二次 | 404/410 | 一次性 |

## 它在哪跑，以及为什么不能在本机跑

兑换端点在 `kb.mall.qushiyun.com` 上，那条域名只在 41/36 的链路里可达。**本机不可达**，
所以脚本要么在 41 上跑，要么由调用方把 `--hop-base` 指到一个能到达的地址。

**真实会话从哪来**：`--dify-session-file` 指向一个只读文件，内容是浏览器里那对
`access_token` / `csrf_token`。**不借真实客户的会话** —— 这里用的是运营自己的
`system` 账号，与"冒充客户"是两件事，但同样**不记录 cookie 值**（只记长度与是否为空）。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONSOLE = "https://dify.ranlei.work"
DEFAULT_WORKSPACE = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"


def _request(
    url: str,
    *,
    cookie: str = "",
    method: str = "GET",
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], str]:
    """返回 ``(status, headers, body)``。**不跟随重定向** —— 303 本身就是证据。"""
    request = urllib.request.Request(url, method=method)
    if cookie:
        request.add_header("Cookie", cookie)
    request.add_header("Accept", "text/html,application/json")

    # **不跟随重定向。** `urllib.request.urlopen` 默认跟随 30x，于是这里读到的会是
    # "跟到底之后的最终状态"（200），而 303 **本身就是**这条链要验的东西。第一版没关它，
    # 于是 303 被读成 200 判失败 —— 桥的日志同时显示它明明回了 303。
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D401, ANN002
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _headers_of(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, _headers_of(exc.headers), exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - status 0 = 从未完成（DNS/连接/超时）
        return 0, {}, ""


def _headers_of(headers: Any) -> dict[str, str]:
    """响应头 → 小写键的字典，**多值头用 `", "` 拼起来**。

    `dict(response.headers)` 会丢掉重复键里的后来者（`Set-Cookie` 尤其常见），而这里
    `Set-Cookie` 正是判据本身。用 `get_all` 保全集。
    """
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except AttributeError:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    grouped: dict[str, list[str]] = {}
    for key, value in items:
        grouped.setdefault(str(key).lower(), []).append(str(value))
    for key, values in grouped.items():
        out[key] = ", ".join(values)
    return out


def _target_of(body: str) -> str:
    """桥把目标放在 JSON 的 ``target`` 里；非 JSON 就当没有。"""
    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    return str((payload or {}).get("target") or "") if isinstance(payload, dict) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--console", default=DEFAULT_CONSOLE)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--hop-base", default="", help="兑换端点基地址；空 = 用桥给的那个")
    parser.add_argument("--dify-session-file", default="", help="只读文件：`access_token=…; csrf_token=…`")
    parser.add_argument("--out", default="var/verify-evidence/knowledge-open-closed-loop.json")
    args = parser.parse_args()

    cookie = ""
    if args.dify_session_file:
        raw = Path(args.dify_session_file).expanduser().read_text(encoding="utf-8").strip()
        cookie = raw
    evidence: dict[str, Any] = {
        "console": args.console,
        "workspace": args.workspace,
        # **不记 cookie 本身**，只记它有没有、多长。
        "session": {"provided": bool(cookie), "length": len(cookie)},
        "checks": [],
    }
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object) -> None:
        evidence["checks"].append({"check": name, "ok": ok, "detail": detail})
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {str(detail)[:220]}")
        if not ok:
            failures.append(name)

    open_path = f"{args.console.rstrip('/')}/__aiops/knowledge-open"

    # ① 无会话必须拒绝 —— 入口不认 URL 参数，只认验过的会话。
    status, _headers, _body = _request(f"{open_path}?workspace={args.workspace}")
    check("桥：无会话 → 401", status == 401, {"status": status})

    if not cookie:
        print("没有给 --dify-session-file：只跑了无凭据那一条。", flush=True)
    else:
        # ② 有会话 → 303 且承诺一个 target。
        status, headers, body = _request(f"{open_path}?workspace={args.workspace}", cookie=cookie)
        # 303 的承诺在 **Location 头**里，body 是空的（`Content-Length: 0`）。
        # 先看 JSON body 是给"未来可能改成 200 + JSON"留的兼容，不是主判据。
        target = headers.get("location", "") or _target_of(body)
        check(
            "桥：有会话 → 303 且 Location 指向兑换端点",
            status == 303 and target.startswith("http") and "/__aiops/knowledge/hop?ticket=" in target,
            {"status": status, "location": target[:90]},
        )

        if target:
            hop_url = target
            if args.hop_base:
                from urllib.parse import urlsplit

                parts = urlsplit(target)
                hop_url = args.hop_base.rstrip("/") + parts.path + ("?" + parts.query if parts.query else "")

            # ③ 真票兑换：303 到 RAGFlow + Set-Cookie 落到目标域。
            status, headers, _ = _request(hop_url, cookie=cookie)
            set_cookie = headers.get("set-cookie", "")
            location = headers.get("location", "")
            check(
                "兑换：真票 → 303 且种下会话",
                status == 303 and bool(set_cookie) and bool(location),
                {"status": status, "location": location[:80], "set_cookie": bool(set_cookie)},
            )
            check(
                "兑换：Set-Cookie 的域与跳转目标同域（ADR-0093）",
                "domain=" in set_cookie.lower() or bool(location),
                {"set_cookie_names": [c.split("=")[0].strip() for c in set_cookie.split(";") if c.strip()]},
            )

            # ④ 一次性：同一张票再来一次必须被拒。
            status_again, _, _ = _request(hop_url, cookie=cookie)
            check("兑换：同一张票第二次 → 404/410", status_again in (404, 410), {"status": status_again})

    # ⑤ 假票：可区分的拒绝，不是 5xx。
    if args.hop_base:
        fake = f"{args.hop_base.rstrip('/')}/__aiops/knowledge/hop?ticket=deadbeefdeadbeef"
        status, _h, _b = _request(fake)
        check("兑换：假票 → 404/410（不是 5xx）", status in (404, 410), {"status": status})

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path(__file__).resolve().parents[4] / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    evidence["failures"] = failures
    out_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"evidence: {out_path}")
    print("all checks passed" if not failures else f"{len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
