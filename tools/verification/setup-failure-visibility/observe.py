"""证明失败的**步骤与异常种类**经公开形状给出，且异常正文不外泄（ADR-0200）。

两条检查，都在一次性数据根里、不接触真实 Dify：

1. 公开形状：`public_entry()` 只按白名单取字段，`failed_step` / `failure_kind` 出现，
   任何非白名单字段（正文、路径）不出现。
2. 真实服务边界：真跑 `aiops-gateway serve`，未带会话读作业进度必须 401 ——
   新字段不是一条新的免鉴权读入口。

不造真实开通失败：那需要可用的 Dify 控制台。失败**归因**的端到端证明在
`tests/dify/test_self_service.py`（逐端、作业级两条路径都有确定性用例）。

    uv run python tools/verification/setup-failure-visibility/observe.py \
        [--out dist/verification/setup-failure-visibility]
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
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_health(port: int, seconds: float = 45.0) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                return response.status
        except Exception:  # noqa: BLE001 - 还没起来就继续等
            time.sleep(0.5)
    return 0


def public_shape() -> dict:
    from aiops_diagnostics.dify.self_service_store import public_entry

    entry = {
        "state": "failed",
        "error_code": "SETUP_STEP_FAILED",
        "failed_step": "operator:dependencies",
        "failure_kind": "ConnectTimeout",
        "app_id": None,
        "published": False,
        # 这三个是**内部**字段，公开形状里一个都不该出现。
        "exception_message": "/etc/aiops-41/consumer-wf01/keys/leak",
        "traceback": "Traceback (most recent call last)",
        "key_ref": "/etc/aiops-41/consumer-wf01/keys/x.key",
    }
    public = public_entry(entry)
    return {
        "fields": sorted(public),
        "failed_step": public.get("failed_step"),
        "failure_kind": public.get("failure_kind"),
        "leaked": [f for f in ("exception_message", "traceback", "key_ref") if f in public],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist/verification/setup-failure-visibility")
    args = parser.parse_args()
    out = (REPO / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    failures: list[str] = []

    shape = public_shape()
    evidence["public_shape"] = shape
    print(f"public shape: {json.dumps(shape, ensure_ascii=False)}")
    if shape["failed_step"] != "operator:dependencies" or shape["failure_kind"] != "ConnectTimeout":
        failures.append("public entry dropped the location fields")
    if shape["leaked"]:
        failures.append(f"public entry leaked internal fields: {shape['leaked']}")

    home = Path(tempfile.mkdtemp(prefix="aiops-failure-"))
    port = free_port()
    config = home / "config"
    config.mkdir(mode=0o700, parents=True)
    (config / "production.env").write_text("# isolated failure-visibility verification\n")
    (config / "production.env").chmod(0o600)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AIOPS_")},
        "AIOPS_HOME": str(home),
        "AIOPS_CONFIG_HOME": str(config),
        "AIOPS_DATA_HOME": str(home / "data"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "AIOPS_GATEWAY_PORT": str(port),
        "PYTHONPATH": str(REPO / "src"),
    }
    process = subprocess.Popen(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        status = wait_health(port)
        evidence["health_status"] = status
        if status != 200:
            failures.append("gateway did not become ready")
        url = f"http://127.0.0.1:{port}/v1/dify/management/operations/00000000-0000-0000-0000-000000000000"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                evidence["anonymous_operation"] = response.status
                failures.append("anonymous operation read was allowed")
        except urllib.error.HTTPError as exc:
            evidence["anonymous_operation"] = exc.code
            # 401/403 都是"没有会话就不给读"；具体用哪个由鉴权层决定，不在本片断言。
            if exc.code not in (401, 403):
                failures.append(f"anonymous operation read returned {exc.code}, not 401/403")
        except urllib.error.URLError as exc:
            # 连接被拒说明进程已退出；启动失败在上面已经记过，这里不重复加一条。
            evidence["anonymous_operation"] = f"unreachable: {exc.reason}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(home, ignore_errors=True)

    evidence["failures"] = failures
    (out / "setup-failure-visibility.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"evidence: {out / 'setup-failure-visibility.json'}")
    print("all checks passed" if not failures else f"FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
