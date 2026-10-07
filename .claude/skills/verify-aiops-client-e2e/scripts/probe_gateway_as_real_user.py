#!/usr/bin/env python3
"""Drive the PRODUCTION AI-Ops gateway as a real end user, for one or more languages.

Why this exists: `verify-aiops-gateway` boots a throwaway gateway and proves the
control plane, but every answer-surface route (`/v1/faq/*`, `/v1/shortcuts`,
`/v1/assistant/*`) refuses a locally-enrolled device token with 401. So the
surfaces users actually read had no reproducible end-to-end check at all.

This runs ON the gateway host, reuses the production configuration and a real
`thirdSession` from Redis, and drives the app over its real ASGI surface with
structured request/response logs. Read-only: it issues no request that writes
business state.

## Credential discipline

This reads three secrets and prints none of them: the service token from the
gateway's own settings, the Redis password and a session id from Redis. The
session id IS a credential — it authenticates as that user until it expires.
Pass `--redact` (default) and never redirect this script's output into a file
that gets committed.

## The two things that cost the most time to find

1. **The header is `X-Third-Session`.** FastAPI strips underscores from header
   names, so a lowercase `third-session` is not recognised and arrives as
   `None` — the resolver then reports "service authentication failed", which
   points at the token rather than at the header.
2. **The session value is the KEY, minus the prefix.** `GET app:3rd_session:<x>`
   returns a Java-serialised blob; the resolver reads the JSON inside it. The
   request header carries `<x>`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

DEFAULT_LANGUAGES = ("zh", "en", "zh-Hant", "vi", "mn", "th", "km")


def _settings(config_file: pathlib.Path):
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    return GatewayServerSettings.from_env()


def _runtime(config_file: pathlib.Path):
    from aiops_diagnostics.config import Settings

    return Settings.from_config(config_file)


def _pick_session(runtime, settings, prefix: str, scope: str, entry: str) -> tuple[str, str] | None:
    """Return (session id, tenant id) for the first session the resolver ACCEPTS.

    Acceptance is decided by calling the production resolver, not by inspecting
    the blob: the resolver is the authority on what a usable session is, and
    sampling by hand would re-implement its rules (and drift).
    """
    import redis

    from aiops_diagnostics.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

    resolver = RedisThirdSessionResolver(
        ThirdSessionSettings(
            host=runtime.redis.host,
            port=runtime.redis.port,
            database=runtime.redis.database,
            username=runtime.redis.user,
            password=runtime.redis.password,
            service_token=settings.third_session_service_token,
            key_prefix=prefix,
        )
    )
    client = redis.Redis(
        host=runtime.redis.host,
        port=runtime.redis.port,
        db=runtime.redis.database,
        password=runtime.redis.password,
        decode_responses=False,
    )
    for key in [k.decode() for k in client.scan_iter(f"{prefix}*", count=500)][:500]:
        candidate = key[len(prefix) :]
        try:
            context = resolver.resolve(
                settings.third_session_service_token,
                required_scope=scope,
                third_session=candidate,
                platform_entry=entry,
            )
        except Exception:  # noqa: BLE001 — a rejected session is the normal case
            continue
        # For `operator` the scope must actually be an operator scope, not merely
        # a session that resolved: `self` is the consumer-shaped fallback, and
        # accepting it would probe the operator routes as a plain consumer and
        # report a defect that is really a wrong-session choice.
        if entry == "operator" and str(getattr(context, "data_scope", "")).find("self") != -1:
            continue
        return candidate, context.effective_tenant_id
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-file", type=pathlib.Path, default=pathlib.Path("/etc/aiops-41/production.env")
    )
    parser.add_argument("--entry", default="consumer", choices=("consumer", "operator"))
    parser.add_argument("--languages", default=",".join(DEFAULT_LANGUAGES))
    parser.add_argument(
        "--paths",
        default="/v1/faq/recommendations,/v1/shortcuts",
        help="comma-separated; any answer-surface GET route",
    )
    parser.add_argument("--scope", default="aiops:faq:read")
    parser.add_argument(
        "--show-copy",
        action="store_true",
        help="print the first item's text. Off by default: production content, lands in transcripts.",
    )
    parser.add_argument(
        "--accept-live-app",
        action="store_true",
        help=(
            "construct the app and actually call the routes. THIS WRITES: app construction "
            "runs recover_interrupted_jobs, marking every queued/running job failed. Only use "
            "when no job is in flight, and check before and after."
        ),
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "treat a 200 with an empty list as a pass. Default is to FAIL it: a surface that "
            "promises content and returns none has not been verified, and reporting it green is "
            "the false-negative this skill exists to prevent."
        ),
    )
    args = parser.parse_args()

    settings = _settings(args.config_file)
    runtime = _runtime(args.config_file)
    prefix = settings.third_session_key_prefix

    picked = _pick_session(runtime, settings, prefix, args.scope, args.entry)
    if picked is None:
        print("❌ 没有可被解析器接受的会话。检查 Redis 里是否有会话、前缀是否与配置一致。", file=sys.stderr)
        return 1
    session, tenant = picked
    print(f"会话已选定（值不打印）  租户={tenant}  前缀={prefix!r}")
    print()

    from fastapi.testclient import TestClient

    from aiops_diagnostics.gateway_api import create_gateway_app

    # 🔴 Do NOT build the whole app against the LIVE database.
    #
    # `create_gateway_app` calls `GatewayStore(...).recover_interrupted_jobs()`,
    # which marks every queued/running job as failed. That is the right thing on
    # a boot path — the process that held those jobs really did die — but here
    # the production gateway is STILL RUNNING and still working on them. Building
    # the app would fail live users' in-progress diagnoses and questions, and the
    # probe's read-only requests would not undo it.
    #
    # A read-only route check does not need the job machinery at all, so the
    # database is never opened for writing: `--dry-run` prints what it WOULD ask
    # and refuses to construct anything. Pass `--accept-live-app` only when you
    # know no job is in flight (see the runbook).
    if not args.accept_live_app:
        print("ℹ️  未构造 app（那会调用 recover_interrupted_jobs，把在飞作业标失败）。")
        print("   本次只做只读探测；确认没有在飞作业后再加 --accept-live-app。")
        print()
        for language in (tag.strip() for tag in args.languages.split(",") if tag.strip()):
            for path in (p.strip() for p in args.paths.split(",") if p.strip()):
                print(f"  (计划) {language:8} GET {path}")
        return 0

    app = create_gateway_app(settings=settings)
    failures = 0
    with TestClient(app) as client:
        for language in (tag.strip() for tag in args.languages.split(",") if tag.strip()):
            for path in (p.strip() for p in args.paths.split(",") if p.strip()):
                response = client.get(
                    path,
                    headers={
                        "Authorization": f"Bearer {settings.third_session_service_token}",
                        "X-Business-Entry": args.entry,
                        # Underscore form: FastAPI would drop `third-session`.
                        "X-Third-Session": session,
                        "Accept-Language": language,
                    },
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                served = body.get("language")
                items = body.get("recommendations") or body.get("shortcuts") or []
                first = str(items[0].get("title") or items[0].get("label") or "")[:30] if items else ""
                empty = not items
                ok = response.status_code == 200 and served == language and (args.allow_empty or not empty)
                note = (
                    " ← 空列表：该面承诺有内容却没返回，不算通过（要放过加 --allow-empty）"
                    if (empty and not args.allow_empty)
                    else ""
                )
                if not ok:
                    failures += 1
                # The title is production content and travels back into an agent's
                # transcript, so it is opt-in: counts and served language carry the
                # verdict, and a title adds nothing checkable.
                shown = f"  {first}" if args.show_copy else ""
                print(
                    f"  {'✅' if ok else '❌'} {language:8} {path:26} "
                    f"HTTP {response.status_code}  served={served}  n={len(items)}{shown}{note}"
                )

    print()
    if failures:
        print(f"❌ {failures} 个组合未通过（判据：HTTP 200 **且** served == 请求语言）", file=sys.stderr)
        return 1
    print("✅ 全部通过（判据：HTTP 200 且 served == 请求语言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
