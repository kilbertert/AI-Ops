"""一次性数据根里真跑 Gateway，证明业务环境清单**在运行时**被读取与校验（ADR-0199）。

两个方向：
  A. 清单合法 ⇒ `serve` 起得来，/health 200；
  B. 清单里一条远端 http 地址 ⇒ `serve` **启动即失败**，而不是起得来、到第一次开通才拒。

不碰真实 AIOPS_DATA_HOME；不写任何令牌。

    uv run python tools/verification/business-environments/observe.py
        [--out dist/verification/business-environments]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def root() -> Path:
    return Path(tempfile.mkdtemp(prefix="aiops-catalogue-"))


def env_for(home: Path, port: int) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("AIOPS_")},
        "AIOPS_HOME": str(home),
        "AIOPS_CONFIG_HOME": str(home / "config"),
        "AIOPS_DATA_HOME": str(home / "data"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "AIOPS_GATEWAY_PORT": str(port),
        "PYTHONPATH": str(REPO / "src"),
    }


def write_catalogue(home: Path, entries: list[dict]) -> Path:
    path = home / "business-environments.json"
    path.write_text(json.dumps({"schema": "aiops-business-environments/1", "environments": entries}))
    path.chmod(0o600)
    return path


def config(home: Path, catalogue: Path) -> None:
    directory = home / "config"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = f"# isolated gateway verification\nAIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE={catalogue}\n"
    (directory / "production.env").write_text(body)
    (directory / "production.env").chmod(0o600)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_health(port: int, seconds: float = 40.0) -> tuple[int, dict]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                return response.status, json.loads(response.read())
        except Exception:  # noqa: BLE001 - 没起来就是没起来，继续等
            time.sleep(0.5)
    return 0, {}


def starts(home: Path) -> tuple[bool, dict]:
    port = free_port()
    process = subprocess.Popen(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=REPO,
        env=env_for(home, port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        status, body = wait_health(port)
        return status == 200, {"status": status, "health": body}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def refuses(home: Path) -> tuple[bool, dict]:
    port = free_port()
    result = subprocess.run(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=REPO,
        env=env_for(home, port),
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode != 0, {
        "returncode": result.returncode,
        "mentions_catalogue": "business environment catalogue is unusable" in output,
        "tail": output[-600:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist/verification/business-environments")
    args = parser.parse_args()
    out = (REPO / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    evidence: dict = {"cases": {}}
    failures: list[str] = []

    good = root()
    try:
        config(good, write_catalogue(good, [{"id": "cpms-verify", "base_url": "https://cpms.example.test"}]))
        ok, detail = starts(good)
        evidence["cases"]["valid_catalogue_starts"] = detail
        print(f"A valid catalogue -> starts={ok}")
        if not ok:
            failures.append("valid catalogue: gateway did not start")
    finally:
        shutil.rmtree(good, ignore_errors=True)

    bad = root()
    try:
        # 远端 http 在 canonical_gateway_url 里是非法的（只允许 HTTPS 或本机回环）。
        config(bad, write_catalogue(bad, [{"id": "cpms-verify", "base_url": "http://cpms.example.test"}]))
        refused, detail = refuses(bad)
        evidence["cases"]["invalid_url_refuses_to_start"] = detail
        print(f"B invalid URL -> refuses={refused} mentions_catalogue={detail['mentions_catalogue']}")
        if not refused:
            failures.append("invalid URL: gateway started anyway")
        if not detail["mentions_catalogue"]:
            failures.append("invalid URL: startup error does not name the catalogue")
    finally:
        shutil.rmtree(bad, ignore_errors=True)

    evidence["failures"] = failures
    (out / "business-environments.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"evidence: {out / 'business-environments.json'}")
    print("all checks passed" if not failures else f"FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
