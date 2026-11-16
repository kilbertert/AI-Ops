"""从实际调用方检查正式网络；匿名HTTP成功不等于业务开通。"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint
from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc

MAX_BODY = 65536


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load(path: Path) -> list[dict]:
    validate_private_file(path)
    if path.stat().st_size > MAX_BODY:
        raise ValueError("manifest too large")
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("expected 1 to 16 checks")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"id", "url", "status", "json_equals"}:
            raise ValueError("invalid check fields")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", identifier):
            raise ValueError("invalid check id")
        if identifier in seen or type(row.get("status")) is not int or not 200 <= row["status"] <= 599:
            raise ValueError("duplicate id or invalid status")
        seen.add(identifier)
        url = row.get("url")
        if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError("invalid endpoint")
        require_secure_endpoint(url, what="preflight")
        if "json_equals" in row and (
            not isinstance(row["json_equals"], dict)
            or any(type(value) not in (str, bool, int, type(None)) for value in row["json_equals"].values())
        ):
            raise ValueError("json_equals must contain scalar strings, booleans, integers or null")
        if row["status"] < 300 and not row.get("json_equals"):
            raise ValueError("successful responses require service identity fields")
    return rows


def probe(row: dict, timeout: float) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        try:
            response = opener.open(row["url"], timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status, raw = response.code, response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return {"passed": False, "http": status, "reason": "response_too_large"}
        passed = status == row["status"] and not 300 <= status <= 399
        if "json_equals" in row:
            body = json.loads(raw)
            passed = (
                passed
                and isinstance(body, dict)
                and all(
                    key in body and type(body[key]) is type(value) and body[key] == value
                    for key, value in row["json_equals"].items()
                )
            )
        return {"passed": passed, "http": status, "reason": "matched" if passed else "unexpected_response"}
    except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
        # 异常和正文可能带地址/敏感数据；只保留分类，不打印原文。
        return {"passed": False, "reason": type(exc).__name__}


def run(rows: list[dict], attempts: int, timeout: float) -> dict:
    checks = [{"id": row["id"], "samples": [probe(row, timeout) for _ in range(attempts)]} for row in rows]
    return {
        "schema": "aiops-network-preflight/1",
        "at": format_stored_time(now_utc()),
        "scope": "anonymous-network-only",
        "passed": all(s["passed"] for c in checks for s in c["samples"]),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="私有JSON：id/url/status/json_equals")
    parser.add_argument("--attempts", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--timeout", type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    try:
        rows = load(args.manifest)
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"passed": False, "reason": "invalid_private_manifest"}))
        return 2
    result = run(rows, args.attempts, args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
