#!/usr/bin/env python3
"""Prove — or honestly fail to prove — that the MODEL-FACING surfaces follow the request language (#566).

The deterministic surfaces (`/v1/faq/*`, `/v1/shortcuts`, the health-report copy
tables) already have end-to-end evidence: the same string goes in and out, and
`probe_gateway_as_real_user.py` can assert `served == requested` mechanically.
This script covers the surfaces where **a model writes the prose**:

    POST /v1/assistant/questions          → poll → result.blocks[].text
    POST /v1/standard/diagnoses           → poll → summary / root_cause / next_steps
    POST /v1/health-report-jobs           → poll → summary / indicator values

For those, "the language is right" is a property of generated text, so the only
honest verdicts are:

* **PASS**   — the run reached a terminal `completed` state AND its prose carries
               no leaking Chinese (for a non-Chinese language).
* **FAIL**   — completed, but Chinese prose is present where it should not be.
* **未验**   — anything that never produced prose: no session, no owned order,
               provider down, job expired, still running past the deadline. These
               are recorded as UNVERIFIED with their cause, **never** as a pass.

Contract identifiers (`code`, `status`, `unit`, `reason_code`) are NOT translated
and their passing through is correct, not a failure — see the shared
`chinese_leak` guard, which is imported from the production tree so the script and
the service judge by the same rule.

## ⚠️ This writes to production, and it runs jobs

Two distinct writes, both deliberate and both why `--run` is opt-in:

1. `create_gateway_app(...)` calls `recover_interrupted_jobs()`, marking every
   `queued`/`running` job failed. Correct on a boot path; **destructive when the
   production gateway is still running and still working**. Check in-flight = 0
   before and after (the runbook's rule).
2. The probes themselves create real jobs and spend real provider quota. Each
   diagnosis takes minutes. `--surfaces` and `--languages` bound the spend.

Default mode issues **no requests at all** and exits 2 — a plan is not a result,
and reading one as the other is exactly the false statement this project keeps
having to remove.

## Credential discipline

Reads the service token, a Redis password and a real session (the session IS a
credential, valid as that user until it expires). Prints none of them, and none
of the model's prose by default: verdicts ride on `chinese_leak` output and the
per-surface status, which are checkable, whereas quoting generated text into an
agent's transcript is not. `--show-copy` opts in.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

#: The question each surface is driven with. Kept short and content-free: the
#: point is which language the ANSWER comes back in, so the ask must not itself
#: be ambiguous. A zero-order question is enough for the QA surface (it needs no
#: order and no retrieval hit to reach a completed state).
QUESTION = {
    "zh": "你好，充电桩一般多久维护一次？",
    "zh-Hant": "你好，充電樁一般多久維護一次？",
    "en": "Hello, how often are charging piles maintained?",
    "de": "Hallo, wie oft werden Ladestationen gewartet?",
    "fr": "Bonjour, à quelle fréquence les bornes sont-elles entretenues ?",
    "es": "Hola, ¿cada cuánto se mantienen los cargadores?",
    "pt": "Olá, com que frequência as estações são mantidas?",
    "vi": "Xin chào, trụ sạc được bảo trì bao lâu một lần?",
    "mn": "Сайн байна уу, цэнэглэгч баганыг хэдэн хугацаанд засварладаг вэ?",
    "th": "สวัสดีครับ สถานีชาร์จดูแลรักษาบ่อยแค่ไหน?",
    "km": "សូមស្វាគមន៍ តើស្ថានីយ៍សាកត្រូវបានថែទាំញឹកញាប់ប៉ុណ្ណា?",
}

DEFAULT_LANGUAGES = ("zh", "en")
ALL_SURFACES = ("qa", "diagnosis", "health_report")

#: Terminal states of the async job families. `expired` is terminal and is NOT a
#: pass: the job produced no prose to judge.
TERMINAL_OK = {"completed"}
TERMINAL_FAIL = {"failed", "cancelled", "expired"}


def _leak_judge():
    """The shared Chinese-leak rule, from the PRODUCTION tree this host serves."""
    from aiops_diagnostics.i18n import chinese_leak

    return chinese_leak


#: Where each surface keeps its terminal payload. Named per surface rather than
#: probed with a chain of `or`: `body.get("result") or body.get("report")` would
#: silently read the wrong envelope the day one surface adds an empty `result`,
#: and the empty-dict falsiness would hide it.
_RESULT_KEY = {"qa": "result", "diagnosis": "result", "health_report": "report"}


def _collect_text(surface: str, body: dict) -> list[str]:
    """Every prose field of a terminal job result, per surface.

    Read by SHAPE, not by walking all strings: a recursive sweep would pick up
    `code` / `status` / `reason_code` and report a leak that is really a contract
    identifier doing its job.

    `indicators[].value` is deliberately EXCLUDED for `health_report`. Measured
    on 41 (2026-10-08): the zh and en reports of the same order carry
    `"value": "拔出断电"` in both — the value is the upstream
    `stopped_reason_content`, stored verbatim, while `summary` and every table
    string are properly localized. Including it here would turn a real defect
    into a blanket FAIL that hides whether the localized prose is clean, which
    is the question this verdict is actually answering. The defect is reported
    separately; see `docs/validation.md` #566.
    """
    result = body.get(_RESULT_KEY[surface]) or {}
    if not isinstance(result, dict):
        return []
    texts: list[str] = []
    if surface == "qa":
        for block in result.get("blocks") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    elif surface == "diagnosis":
        for key in ("summary", "root_cause", "conclusion"):
            if isinstance(result.get(key), str):
                texts.append(result[key])
        for step in result.get("next_steps") or []:
            if isinstance(step, str):
                texts.append(step)
        for hyp in result.get("hypotheses") or []:
            if isinstance(hyp, dict) and isinstance(hyp.get("explanation"), str):
                texts.append(hyp["explanation"])
    elif surface == "health_report" and isinstance(result.get("summary"), str):
        texts.append(result["summary"])
    return texts


def _indicator_leaks(body: dict) -> list[str]:
    """Chinese carried by `indicators[].value` — the defect measured on 41.

    Reported beside the verdict, never folded into it: an upstream value copied
    through verbatim is a genuine language defect, but it says nothing about
    whether the server's own composed copy is in the right language.
    """
    report = body.get(_RESULT_KEY["health_report"]) or {}
    if not isinstance(report, dict):
        return []
    leak = _leak_judge()
    return [
        str(ind.get("value"))
        for ind in report.get("indicators") or []
        if isinstance(ind, dict) and isinstance(ind.get("value"), str) and leak(str(ind["value"]))
    ]


def _verdict(surface: str, status: str, body: dict, language: str) -> tuple[str, str]:
    """One observation -> (`PASS` | `FAIL` | `UNVERIFIED`, human-readable note).

    The verdict judges only the copy the SERVER composes. A Chinese value the
    upstream supplied and the server copied through is reported beside it (see
    `_indicator_leaks`), because folding the two together would make a real
    defect indistinguishable from a localized-prose failure.
    """
    if status in TERMINAL_FAIL:
        return "UNVERIFIED", f"作业终态 {status}，没有产出可判的文案"
    if status not in TERMINAL_OK:
        return "UNVERIFIED", f"轮询超时，仍是 {status}"
    texts = _collect_text(surface, body)
    if not texts:
        return "UNVERIFIED", "终态 completed 但按该面形状取不到任何正文（形状变了？）"
    leak = _leak_judge()
    upstream = _indicator_leaks(body) if surface == "health_report" else []
    tail = f"；另有 {len(upstream)} 个指标 value 是上游原值含汉字（另记，不并入本判定）" if upstream else ""
    if language in {"zh", "zh-Hant"}:
        # Chinese is the authority: a leak judgement would be meaningless.
        return "PASS", f"completed，正文 {len(texts)} 段"
    dirty = {text: leak(text) for text in texts if leak(text)}
    if dirty:
        worst = max(dirty.items(), key=lambda kv: len(kv[1]))
        return "FAIL", f"completed，但 {len(dirty)}/{len(texts)} 段含汉字（例：{worst[1][:12]}）{tail}"
    return "PASS", f"completed，正文 {len(texts)} 段零汉字残留{tail}"


def _poll(client, job_path: str, headers: dict, *, deadline_s: int) -> tuple[str, dict]:
    """Poll a job route until terminal or the deadline. Returns (status, body)."""
    end = time.monotonic() + deadline_s
    body: dict = {}
    status = "timeout"
    while time.monotonic() < end:
        response = client.get(job_path, headers=headers)
        if response.status_code != 200:
            return f"http_{response.status_code}", {}
        try:
            body = response.json()
        except ValueError:
            return "bad_json", {}
        status = str(body.get("status") or "")
        if status in TERMINAL_OK | TERMINAL_FAIL:
            return status, body
        time.sleep(2)
    return status or "timeout", body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config-file", type=pathlib.Path, default=pathlib.Path("/etc/aiops-41/production.env")
    )
    parser.add_argument("--languages", default=",".join(DEFAULT_LANGUAGES))
    parser.add_argument("--surfaces", default=",".join(ALL_SURFACES))
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "actually drive the surfaces. THIS WRITES: app construction runs "
            "recover_interrupted_jobs, and the probes create real jobs that spend "
            "provider quota. Require in-flight jobs = 0 before and after."
        ),
    )
    parser.add_argument("--show-copy", action="store_true", help="print model prose (off by default)")
    parser.add_argument("--deadline", type=int, default=600, help="per-job poll deadline, seconds")
    args = parser.parse_args()

    languages = [tag.strip() for tag in args.languages.split(",") if tag.strip()]
    surfaces = [name.strip() for name in args.surfaces.split(",") if name.strip()]
    for name in surfaces:
        if name not in ALL_SURFACES:
            print(f"未知面：{name}（可选 {', '.join(ALL_SURFACES)}）", file=sys.stderr)
            return 2

    if not args.run:
        print("计划（未发任何请求，故未取证；退出码 2）：")
        print(f"  语言：{', '.join(languages)}")
        print(f"  面  ：{', '.join(surfaces)}")
        print("  每门语言 × 每个面：发起一次真实请求并轮询到终态，判定正文语言")
        print("  判据：completed 且（中文语言 ∨ 零汉字残留）⇒ PASS；其余一律 UNVERIFIED，不记通过")
        print("  ⚠️ --run 会写：构造 app 会 recover_interrupted_jobs；探测本身创建真实作业")
        return 2

    return run(args, languages, surfaces)


def run(args, languages: list[str], surfaces: list[str]) -> int:
    """The live path: resolve a session, resolve an owned order, drive the surfaces."""
    from fastapi.testclient import TestClient

    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings.from_env()

    session, tenant = _pick_session(settings)
    if session is None:
        print("❌ 没有可被生产解析器接受的会话 —— 本票记 未验，不记通过", file=sys.stderr)
        return 1
    print(f"会话已选定（值不打印）  租户={tenant}")

    order_no = _pick_order(settings, session) if {"diagnosis", "health_report"} & set(surfaces) else None
    if order_no is None and {"diagnosis", "health_report"} & set(surfaces):
        print("⚠️  该会话名下没有可用订单：诊断面与健康报告面记 未验")
    else:
        print("订单已选定（值不打印）")

    app = create_gateway_app(settings=settings)
    headers = {
        "Authorization": f"Bearer {settings.third_session_service_token}",
        "X-Business-Entry": "consumer",
        # Underscore form: FastAPI drops `third-session` and the resolver then
        # reports "service authentication failed", which points at the token.
        "X-Third-Session": session,
    }

    rows: list[tuple[str, str, str, str]] = []
    with TestClient(app) as client:
        for language in languages:
            h = {**headers, "Accept-Language": language}
            for surface in surfaces:
                verdict, note = _drive(client, surface, h, language, order_no, args)
                rows.append((language, surface, verdict, note))
                print(f"  {verdict:11} {language:8} {surface:14} {note}")

    print()
    for verdict in ("PASS", "FAIL", "UNVERIFIED"):
        n = sum(1 for row in rows if row[2] == verdict)
        print(f"  {verdict}: {n}")
    print()
    print("判据：UNVERIFIED 不是通过。它写的是「这一门没有产出可判的文案」，原因见上。")
    return 1 if any(row[2] == "FAIL" for row in rows) else 0


def _drive(client, surface: str, headers: dict, language: str, order_no: str | None, args) -> tuple[str, str]:
    """One (language, surface) observation."""
    if surface == "qa":
        response = client.post(
            "/v1/assistant/questions",
            headers=headers,
            json={"question": QUESTION.get(language, QUESTION["en"])},
        )
        if response.status_code != 202:
            return "UNVERIFIED", f"创建返回 {response.status_code}"
        qa_id = str(response.json().get("qa_id") or "")
        if not qa_id:
            return "UNVERIFIED", "创建返回 202 但没有 qa_id"
        status, body = _poll(client, f"/v1/assistant/questions/{qa_id}", headers, deadline_s=args.deadline)
        return _verdict("qa", status, body, language)

    if order_no is None:
        return "UNVERIFIED", "该会话名下没有可用订单，无法发起"

    if surface == "diagnosis":
        response = client.post(
            "/v1/standard/diagnoses",
            headers=headers,
            json={"order_no": order_no, "question": QUESTION.get(language, QUESTION["en"])},
        )
        if response.status_code != 202:
            return "UNVERIFIED", f"创建返回 {response.status_code}"
        diagnosis_id = str(response.json().get("diagnosis_id") or "")
        if not diagnosis_id:
            return "UNVERIFIED", "创建返回 202 但没有 diagnosis_id"
        status, body = _poll(
            client, f"/v1/standard/diagnoses/{diagnosis_id}", headers, deadline_s=args.deadline
        )
        return _verdict("diagnosis", status, body, language)

    if surface == "health_report":
        response = client.post("/v1/health-report-jobs", headers=headers, json={"order_no": order_no})
        if response.status_code != 202:
            return "UNVERIFIED", f"创建返回 {response.status_code}"
        job_id = str(response.json().get("job_id") or "")
        if not job_id:
            return "UNVERIFIED", "创建返回 202 但没有 job_id"
        status, body = _poll(client, f"/v1/health-report-jobs/{job_id}", headers, deadline_s=args.deadline)
        return _verdict("health_report", status, body, language)

    return "UNVERIFIED", f"未知面 {surface}"


def _pick_session(settings) -> tuple[str | None, str]:
    """First session the PRODUCTION resolver accepts for this scope.

    Acceptance is the resolver's call, not ours: sampling the blob by hand would
    re-implement its rules and drift from them.
    """
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
        except Exception:  # noqa: BLE001 — a rejected session is the normal case
            continue
        return candidate, context.effective_tenant_id
    return None, ""


def _pick_order(settings, session: str) -> str | None:
    """An order the SESSION's own user owns — the authorizer will check again."""
    import pymysql

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
    context = resolver.resolve(
        settings.third_session_service_token,
        required_scope="aiops:diagnoses:write",
        third_session=session,
        platform_entry="consumer",
    )
    user_id = getattr(context.subject, "c_user_id", None)
    if not user_id:
        return None
    connection = pymysql.connect(
        host=runtime.mysql.host,
        port=runtime.mysql.port,
        user=runtime.mysql.user,
        password=runtime.mysql.password,
        database=runtime.mysql.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    with connection, connection.cursor() as cur:
        cur.execute(
            """select order_no from ch_order_info
               where user_id = %s and status = 1
               order by stop_time desc limit 1""",
            (str(user_id),),
        )
        row = cur.fetchone()
    return str(row["order_no"]) if row else None


if __name__ == "__main__":
    raise SystemExit(main())
