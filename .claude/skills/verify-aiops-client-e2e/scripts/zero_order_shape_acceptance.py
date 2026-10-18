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

#: A zero-order question: no order number, not an FAQ hit, no shortcut code —
#: and, critically, CASUAL. A greeting is what the classifier routes to
#: `skip_retrieval`, which is the only way to be sure the request reaches
#: `run_zero_order_answer`: every other route can be answered by the RAG path,
#: the promotional cards, or the "unmapped tenant" card, all of which already
#: emitted blocks -- so a probe using them would PASS without ever exercising
#: the code under test. (The first version of this probe made exactly that
#: mistake: it used a maintenance question and, on a tenant absent from the
#: runtime registry, got the `_unmapped_tenant_result` card and reported PASS.)
QUESTION = {
    "zh": "你好",
    "en": "Hello",
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


def _fallback_copy() -> str:
    """The zero-order `unavailable` fallback string, or "" if unreadable.

    Imported from the deployed package rather than copied: this string is what
    `_guard_zero_order_language` substitutes, and a hand-copied duplicate would
    drift the moment the copy is reworded — silently turning this check back
    into the false PASS it exists to prevent.
    """
    try:
        from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, QA_FALLBACK_MESSAGES

        pack = QA_FALLBACK_MESSAGES.get("zh") or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
        return str(pack.get("unavailable") or "")
    except Exception:  # noqa: BLE001 - the probe degrades, it does not crash
        return ""


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
        #
        # Kept honest about its own scope: only the zero-order path ever wrote
        # this shape, so a `{text}` row here IS the regression. But the verdict
        # names what it observed rather than asserting a cause it did not verify
        # — if some future route also emits a bare `text`, this line should be
        # re-read rather than trusted as a zero-order statement (devin review,
        # #830). No route marker can settle it: `searches` is a RAG marker and
        # this shape carries no markers at all, which is the whole point.
        if isinstance(result.get("text"), str) and result["text"].strip():
            return "FAIL", "result 是旧形状 {text,reminder} —— blocks[] 客户端读不到正文"
        return "UNVERIFIED", f"result 既无 blocks 也无 text（键：{sorted(result)}）"
    # Route proof, and it has to come BEFORE the shape verdict: several other
    # paths also emit blocks[], so "has blocks" alone would PASS without the
    # plain zero-order path ever having run — the probe would report success for
    # a question that never exercised the code under test.
    #
    # Two independent signals, both cheap and both derived from the runtime's
    # own records rather than from our guess about routing:
    #
    #   `searches` — written only by the RAG/promo seams (qa_rag._finalize,
    #     promo_agents); the zero-order path never writes it. Its presence means
    #     a knowledge search ran, i.e. this was not the zero-order path.
    #   `retrieval_status` — for a CASUAL question nothing is searched, so the
    #     honest value is `not_found`. `unavailable` means the answer is degraded
    #     copy produced because the knowledge base was unreachable, and the
    #     "unmapped tenant" card uses it too; neither is a model answer, so
    #     neither is evidence that a zero-order answer renders.
    if "searches" in result:
        return "UNVERIFIED", (
            "这条由知识库路径作答（result 带 searches），没有走到零阶路径 —— 判据不适用，换一条寒暄类问题"
        )
    if result.get("retrieval_status") != "not_found":
        return "UNVERIFIED", (
            f"retrieval_status={result.get('retrieval_status')!r} 不是 not_found —— "
            "寒暄问题本不该检索，这个值说明回答来自降级兜底（知识库故障）或"
            "未登记租户卡片，不是零阶模型正文，不能作为通过证据"
        )
    kinds = [b.get("kind") for b in blocks if isinstance(b, dict)]
    if len(kinds) != len(blocks) or any(k is None for k in kinds):
        return "FAIL", "blocks 里有块缺 kind"
    texts = [b.get("text") for b in blocks if b.get("kind") == "text"]
    if not texts or not any(t and t.strip() for t in texts):
        return "FAIL", "blocks 里没有非空 text 块"
    if "text" in result:
        return "FAIL", "result 同时带顶层 text 键 —— 两种形状并存"
    # The language guard (`_guard_zero_order_language`) swaps a leaking answer
    # for the localized fallback copy and deliberately keeps `retrieval_status`
    # as it was, so a leaked-then-replaced turn still reads `not_found` and would
    # PASS above. Its copy is a fixed string, so it is matchable: a body equal to
    # that sentinel is an outage notice with a model's answer nowhere in it, not
    # evidence that a zero-order answer renders.
    sentinel = _fallback_copy()
    if sentinel and any(t.strip() == sentinel for t in texts):
        return "UNVERIFIED", (
            "正文等于语言守卫的兜底文案 —— 模型答了但被语言守卫替换（或本回合就是降级兜底），"
            "这不是模型正文，不能作为通过证据"
        )
    return "PASS", (
        f"零阶路径 blocks={len(blocks)} kinds={kinds} retrieval_status={result.get('retrieval_status')!r}"
    )


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
        "Authorization": f"Bearer {settings.third_session_service_token}",
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
    # Exit code has three states, not two: a run where NOTHING was judged must
    # not look like a pass to whatever called this (devin review, #828). "No
    # PASS" is not "PASS": 0 means at least one language passed and none failed.
    if any(row[1] == "FAIL" for row in rows):
        return 1
    if not any(row[1] == "PASS" for row in rows):
        print("❌ 没有任何一门产出可判的正文 —— 记未验，不记通过", file=sys.stderr)
        return 1
    return 0


def _self_check() -> None:
    """The verdict function against inputs whose answer is known by hand.

    It decides PASS/FAIL/UNVERIFIED and is the only thing standing between "the
    fix works" and a green check, so it is exercised without a network: a
    mistake here would report the defect as fixed.
    """
    comp = "completed"
    blocks_text = {"blocks": [{"kind": "text", "text": "正文"}], "retrieval_status": "not_found"}

    def card(copy: str) -> dict:
        """A degraded card: blocks[] present, but nothing was answered."""
        return {"blocks": [{"kind": "text", "text": copy}], "retrieval_status": "unavailable"}

    def guarded(copy: str) -> dict:
        """What the language guard leaves: fallback copy, status untouched."""
        return {"blocks": [{"kind": "text", "text": copy}], "retrieval_status": "not_found"}

    sentinel = _fallback_copy()
    cases = [
        # (status, result, expected verdict)
        (comp, blocks_text, "PASS"),
        (comp, {**blocks_text, "searches": 1}, "UNVERIFIED"),  # RAG answered, not zero-order
        # The card paths that already emitted blocks before #708 — both would
        # have read as PASS and hidden the defect this probe exists to catch:
        # the "unmapped tenant" card,
        (comp, card("当前知识库暂时不可用，本次回答无法基于知识库确认，请稍后重试。"), "UNVERIFIED"),
        # and the degraded copy written when the knowledge base is unreachable.
        (comp, card("降级文案"), "UNVERIFIED"),
        (comp, {"text": "正文", "reminder": True}, "FAIL"),  # the pre-#708 shape
        (comp, {"blocks": [{"kind": "text", "text": "  "}], "retrieval_status": "not_found"}, "FAIL"),
        (comp, {**blocks_text, "text": "并存"}, "FAIL"),
        (comp, {}, "UNVERIFIED"),
        ("failed", blocks_text, "UNVERIFIED"),
        (comp, {"blocks": [{"text": "缺 kind"}], "retrieval_status": "not_found"}, "FAIL"),
    ]
    # The language-guard case only means something when the sentinel is
    # readable; asserting it unconditionally would make the self-check fail
    # for a reason that has nothing to do with the verdict logic.
    if sentinel:
        cases.append((comp, guarded(sentinel), "UNVERIFIED"))
    for status, result, want in cases:
        got, note = _verdict(status, {"result": result})
        assert got == want, f"verdict({status}, {result}) = {got}, 期望 {want}（{note}）"
    print(f"自检通过：{len(cases)} 种输入（兜底文案哨兵{'已读到' if sentinel else '读不到，跳过该组'}）")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
        raise SystemExit(0)
    raise SystemExit(main())
