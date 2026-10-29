#!/usr/bin/env python3
"""Run aiops-gateway on a throwaway data root and capture HTTP evidence.

Verification scaffolding, not product code. It exists so a verification run cannot
touch the real ``AIOPS_DATA_HOME``: a fresh temp root is created per run, the
gateway is started against it, evidence is written outside it, and the temp root
is removed on the way out.

    uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py

Proves locally: boot, health, current authentication, public schema and retired routes.
Does not prove remote Dify/Java execution, model quality or knowledge availability.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 0


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def http(
    url: str,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    timeout: float = 6.0,
) -> tuple[int, str]:
    """Return (status, body). Status 0 means the request never completed."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception:
        return 0, ""


def run_uv(args: list[str], env: dict[str, str], cwd: Path, timeout: float = 60.0) -> str:
    result = subprocess.run(
        ["uv", "run", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout + result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="var/verify-evidence")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    repo = repo_root()
    root = Path(tempfile.mkdtemp(prefix="aiops-verify-"))
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("AIOPS_")},
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        # The deployment identity (#879). `serve` refuses to start without one, by
        # design — a gateway that does not know which environment it is cannot answer
        # "which workspace does this tenant use". A verification root IS its own
        # deployment, so it gets its own identity; it is never the production value.
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    if args.port == 0:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            args.port = probe.getsockname()[1]
    env["AIOPS_GATEWAY_PORT"] = str(args.port)
    config = root / "config" / "production.env"
    config.parent.mkdir(mode=0o700)
    config.write_text("# isolated gateway verification\n")
    config.chmod(0o600)
    base = f"http://127.0.0.1:{args.port}"
    out_dir = (repo / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {"base": base, "temp_root": str(root)}
    failures: list[str] = []

    print(f"temp root: {root}")
    print(f"gateway  : {base}")

    process = subprocess.Popen(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    try:
        deadline = time.monotonic() + args.startup_timeout
        status, body = 0, ""
        while time.monotonic() < deadline:
            status, body = http(f"{base}/health")
            if status:
                break
            time.sleep(0.5)

        if not status:
            print("FAIL: gateway never became ready", file=sys.stderr)
            return 1

        print(f"  /health -> {status}")
        try:
            health = json.loads(body)
        except ValueError:
            health = {}
        evidence["health"] = health
        if health.get("business_mutations") != "disabled":
            # A safety invariant of this service, not a configuration detail.
            failures.append("business_mutations is not 'disabled' — safety invariant broken")
        if health.get("ok") is not True:
            failures.append("/health did not report ok=true")

        checks = (
            ("/v1/assistant/questions", None, 401),
            ("/v1/agents", None, 401),
            ("/v1/openapi.json", None, 200),
            ("/v1/runs", None, 404),
            ("/v1/enroll", {"code": "retired"}, 404),
            ("/v1/standard/diagnoses", {"order_no": "retired", "question": "test"}, 404),
            ("/v1/dify/tools/order_snapshot", {}, 404),
        )
        for path, payload, expected in checks:
            status, body = http(f"{base}{path}", payload=payload)
            evidence[path] = {"status": status, "body": body if len(body) < 1000 else "omitted"}
            print(f"  {path} -> {status} (expected {expected})")
            if status != expected:
                failures.append(f"{path}: expected {expected}, got {status}")

        (out_dir / "aiops-gateway.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"  evidence: {(out_dir / 'aiops-gateway.json').relative_to(repo)}")

        if failures:
            print("\nFAILURES:", file=sys.stderr)
            for item in failures:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("all checks passed")
        return 0
    finally:
        # Kill what we started, never by name: aiops-gateway may also be a
        # systemd unit on this host.
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        if args.keep_root:
            print(f"temp root kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
        print("done (evidence is outside the temp root and survives)")


if __name__ == "__main__":
    raise SystemExit(main())
