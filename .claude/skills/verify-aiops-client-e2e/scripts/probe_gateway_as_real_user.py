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


def _pick_session(runtime, settings, prefix: str, scope: str) -> tuple[str, str] | None:
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
                platform_entry="consumer",
            )
        except Exception:  # noqa: BLE001 — a rejected session is the normal case
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
    args = parser.parse_args()

    settings = _settings(args.config_file)
    runtime = _runtime(args.config_file)
    prefix = settings.third_session_key_prefix

    picked = _pick_session(runtime, settings, prefix, args.scope)
    if picked is None:
        print("❌ 没有可被解析器接受的会话。检查 Redis 里是否有会话、前缀是否与配置一致。", file=sys.stderr)
        return 1
    session, tenant = picked
    print(f"会话已选定（值不打印）  租户={tenant}  前缀={prefix!r}")
    print()

    from fastapi.testclient import TestClient

    from aiops_diagnostics.gateway_api import create_gateway_app

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
                ok = response.status_code == 200 and served == language
                if not ok:
                    failures += 1
                print(
                    f"  {'✅' if ok else '❌'} {language:8} {path:26} "
                    f"HTTP {response.status_code}  served={served}  n={len(items)}  {first}"
                )

    print()
    if failures:
        print(f"❌ {failures} 个组合未通过（判据：HTTP 200 **且** served == 请求语言）", file=sys.stderr)
        return 1
    print("✅ 全部通过（判据：HTTP 200 且 served == 请求语言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
