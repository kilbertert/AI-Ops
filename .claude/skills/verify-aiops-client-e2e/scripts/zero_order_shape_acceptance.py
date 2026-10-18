#!/usr/bin/env python3
"""#708 部署后的真实验收：零阶回答的 result 形状（在 41 上跑，按需）。

**为什么单独写一个**：`verify_model_surface_language.py` 的 QA 面判据是「散文的
语言」，它在 `_collect_text` 里只走 `blocks[]` —— 旧的 `{text}` 形状取不到正文，
于是它会把一条**形状正确**的回答报成 `UNVERIFIED`。本脚本问的是另一个问题：
**部署出去的网关此刻给客户端的是哪个形状**，以及正文有没有到。

它复用同一个会话选择与同一组请求头（生产配置 + 真实 thirdSession），
只把判据换成形状判据：

  PASS   completed 且 result 有非空 blocks[]、每个块有 kind、
         首个 text 块非空、无顶层 text 键
  FAIL   completed 但 result 是 {text,...} 旧形状（正文在但客户端读不到）
  UNVERIFIED  其余：没有会话、作业 failed/expired、轮询超时

UNVERIFIED 不是通过。

用法（在 41 上，root）：
  cd /opt/aiops-41 && runuser -u aiops41 -- env \
    $(tr '\\0' '\\n' < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
      | grep -E '^AIOPS_' | xargs -d'\\n') \
    /opt/aiops-41/.venv/bin/python /tmp/zero_order_shape_acceptance.py --run --languages zh,en
"""

from __future__ import annotations

import argparse
import json
import sys
import time

#: A zero-order question: no order number, not an FAQ hit, no shortcut code.
QUESTION = {
    "zh": "你好，充电桩一般多久维护一次？",
    "en": "Hello, how often are charging piles maintained?",
}

TERMINAL = {"completed", "failed", "cancelled", "expired"}


def _pick_session(settings) -> tuple[str | None, str]:
    """First session the PRODUCTION resolver accepts for this scope.

    The same rule `verify_model_surface_language.py` uses — acceptance is the
    resolver's call, not ours: sampling the blob by hand would re-implement its
    rules and drift from them.
    """
    import pathlib

    import redis

    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.third_session_auth import RedisThirdSessionResolver, ThirdSessionSettings

    runtime = Settings.from_config(pathlib.Path("/etc/aiops-41/production.env"))
    resolver = RedisThirdSessionResolver(
        ThirdSessionSettings(
            host=runtime.redis.host,
            port=runtime.redis.port,
            database=runtime.redis.database,
            username=runtime.redis.user,
            password=runtime.redis.password,
            service_token=settings.third_session_service_token,
            key_prefix=settings.third_session_key_prefix,
        )
    )
    client = redis.Redis(
        host=runtime.redis.host,
        port=runtime.redis.port,
        db=runtime.redis.database,
        password=runtime.redis.password,
        decode_responses=False,
    )
    prefix = settings.third_session_key_prefix
    for key in [k.decode() for k in client.scan_iter(f"{prefix}*", count=500)][:500]:
        candidate = key[len(prefix) :]
        try:
            context = resolver.resolve(
                settings.third_session_service_token,
                required_scope="aiops:diagnoses:write",
                third_session=candidate,
                platform_entry="consumer",
            )
        except Exception:  # noqa: BLE001 - a rejected session is the normal case
            continue
        return candidate, context.effective_tenant_id
    return None, ""


def _verdict(status: str, body: dict) -> tuple[str, str]:
    if status in {"failed", "cancelled", "expired"}:
        return "UNVERIFIED", f"作业终态 {status}，没有产出可判的正文"
    if status != "completed":
        return "UNVERIFIED", f"轮询超时，仍是 {status}"
    result = body.get("result") or {}
    if not isinstance(result, dict) or not result:
        return "UNVERIFIED", "completed 但 result 为空"
    blocks = result.get("blocks")
    if not blocks:
        # The pre-#708 shape. The text is present but no blocks[] client can
        # read it — this is the defect, stated as a verdict rather than a crash.
        if isinstance(result.get("text"), str) and result["text"].strip():
            return "FAIL", "result 是旧形状 {text,reminder} —— blocks[] 客户端读不到正文"
        return "UNVERIFIED", f"result 既无 blocks 也无 text（键：{sorted(result)}）"
    kinds = [b.get("kind") for b in blocks if isinstance(b, dict)]
    if len(kinds) != len(blocks) or any(k is None for k in kinds):
        return "FAIL", "blocks 里有块缺 kind"
    texts = [b.get("text") for b in blocks if b.get("kind") == "text"]
    if not texts or not any(t and t.strip() for t in texts):
        return "FAIL", "blocks 里没有非空 text 块"
    if "text" in result:
        return "FAIL", "result 同时带顶层 text 键 —— 两种形状并存"
    return "PASS", f"blocks={len(blocks)} kinds={kinds} retrieval_status={result.get('retrieval_status')!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--languages", default="zh,en")
    parser.add_argument("--deadline", type=int, default=600)
    parser.add_argument("--run", action="store_true", help="真的发请求（会写作业行、花额度）")
    parser.add_argument("--show-copy", action="store_true")
    args = parser.parse_args()

    languages = [tag.strip() for tag in args.languages.split(",") if tag.strip()]
    if not args.run:
        print("计划（未发请求，退出码 2）：每门语言发起一次零阶提问并轮询到终态，")
        print("判据：completed 且 result 有非空 blocks[]、块有 kind、无顶层 text 键。")
        return 2

    from fastapi.testclient import TestClient

    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings.from_env()
    session, tenant = _pick_session(settings)
    if session is None:
        print("❌ 没有可被生产解析器接受的会话 —— 记未验，不记通过", file=sys.stderr)
        return 1
    print(f"会话已选定（值不打印）  租户={tenant}")

    app = create_gateway_app(settings=settings)
    headers = {
        "Authorization": f"Bearer {settings.third_session.service_token}",
        "X-Business-Entry": "consumer",
        "X-Third-Session": session,
    }

    rows: list[tuple[str, str, str]] = []
    with TestClient(app) as client:
        for language in languages:
            h = {**headers, "Accept-Language": language}
            question = QUESTION.get(language, QUESTION["en"])
            created = client.post("/v1/assistant/questions", headers=h, json={"question": question})
            if created.status_code != 202:
                rows.append((language, "UNVERIFIED", f"创建返回 {created.status_code}"))
                print(f"  UNVERIFIED {language:6} 创建返回 {created.status_code}")
                continue
            qa_id = str(created.json().get("qa_id") or "")
            status, body = "timeout", {}
            end = time.monotonic() + args.deadline
            while time.monotonic() < end:
                poll = client.get(f"/v1/assistant/questions/{qa_id}", headers=h)
                if poll.status_code != 200:
                    status = f"http_{poll.status_code}"
                    break
                body = poll.json()
                status = str(body.get("status") or "")
                if status in TERMINAL:
                    break
                time.sleep(2)
            verdict, note = _verdict(status, body)
            rows.append((language, verdict, note))
            print(f"  {verdict:11} {language:6} qa_id={qa_id} {note}")
            if args.show_copy:
                print(f"      copy: {json.dumps(body.get('result'), ensure_ascii=False)[:300]}")

    print()
    for verdict in ("PASS", "FAIL", "UNVERIFIED"):
        print(f"  {verdict}: {sum(1 for row in rows if row[1] == verdict)}")
    print("\nUNVERIFIED 不是通过 —— 它写的是「这一门没有产出可判的正文」。")
    return 1 if any(row[1] == "FAIL" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
