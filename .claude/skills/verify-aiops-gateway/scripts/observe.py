#!/usr/bin/env python3
"""Run aiops-gateway on a throwaway data root and capture HTTP evidence.

Verification scaffolding, not product code. It exists so a verification run cannot
touch the real ``AIOPS_DATA_HOME``: a fresh temp root is created per run, the
gateway is started against it, evidence is written outside it, and the temp root
is removed on the way out.

    uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py

Proves, locally: boot, /health contract, unauthenticated 401, enrollment
(code -> device token), and which authenticated routes that token unlocks.
Does NOT prove: /diag/*, model-backed runs, KB liveness — those need remote
services. See the skill's "Known limits".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8787


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
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
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
        **os.environ,
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(
            os.pathsep
        ),
    }
    base = f"http://127.0.0.1:{args.port}"
    out_dir = (repo / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {"base": base, "temp_root": str(root)}
    failures: list[str] = []

    print(f"temp root: {root}")
    print(f"gateway  : {base}")

    # `aiops init` is REQUIRED first: without it `serve` dies with
    # "gateway server production.env does not exist", which reads like a broken
    # checkout rather than a missing setup step.
    run_uv(["aiops", "init"], env, repo)

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

        # An unauthenticated 401 is the CORRECT answer; a 200 would mean the auth
        # gate is not wired.
        status, _ = http(f"{base}/v1/runs")
        print(f"  unauthenticated /v1/runs -> {status} (401 expected)")
        evidence["unauthenticated_runs_status"] = status
        if status != 401:
            failures.append(f"unauthenticated /v1/runs returned {status}, expected 401")

        # A code is SINGLE-USE, so mint one per enrollment.
        minted = run_uv(["aiops-gateway", "issue-enrollment", "--workspace", "ops"], env, repo)
        code = ""
        for line in minted.splitlines():
            if "enr_" in line:
                code = "enr_" + line.split("enr_", 1)[1].split('"', 1)[0]
                break
        print(f"  issued enrollment code: {'yes' if code else 'NO'}")

        if code:
            # All three fields are required and the model forbids extras.
            status, body = http(
                f"{base}/v1/enroll",
                payload={"code": code, "device_name": "verify-probe", "platform": "linux"},
            )
            print(f"  POST /v1/enroll -> {status}")
            try:
                enrolled = json.loads(body)
            except ValueError:
                enrolled = {}
            token = enrolled.get("token", "")
            evidence["enroll_status"] = status
            # Never persist the token itself.
            evidence["enroll_device_id"] = enrolled.get("device_id", "")
            if status != 201 or not token:
                failures.append(f"enrollment failed: {status} {body[:160]}")

            if token:
                for path in ("/v1/runs", "/v1/agents"):
                    status, body = http(f"{base}{path}", token=token)
                    evidence[f"authenticated{path}"] = status
                    print(f"  authenticated {path} -> {status}")
                # /v1/agents legitimately 401s for a device token: agent routes
                # need a UPMS role. Recorded, not treated as a failure.
        else:
            failures.append("could not parse an enrollment code from issue-enrollment output")

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
