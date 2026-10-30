#!/usr/bin/env python3
"""在一次性数据根跑真实 Gateway，验证双端管理面的**关闭态**与鉴权边界。

验证脚手架，不是产品代码。它回答的是这个改动引入的新端点在生产前置条件不具备时
**是否会放行**：答案必须是"不"。响应形状与业务行为由 `tests/dify/test_dual_entry.py`
的契约用例证明，这里证明的是"没有平台凭据/没有配好 profile 时，谁都进不来，
而且一次请求都不会落成开通任务"。

    uv run python tools/verification/check_dual_entry_management.py --out var/verify-evidence

证明：进程启动、健康只读、管理面匿名/错误凭据被拒、请求不产生任务行。
不证明：真实 Dify 上的复制/凭据申请、真实消费者与员工会话、41 上的部署。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MANAGEMENT = "/v1/dify/management"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def http(url: str, token: str | None = None, payload: dict | None = None, timeout: float = 6.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
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


def jobs_count(database: Path) -> int | None:
    """开通任务行数。表不存在 ⇒ 0（这个进程从没写过）。"""
    if not database.exists():
        return 0
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='operator_setup_jobs'"
        ).fetchone()
        if row is None:
            return 0
        return int(db.execute("SELECT count(*) FROM operator_setup_jobs").fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="var/verify-evidence")
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    repo = repo_root()
    root = Path(tempfile.mkdtemp(prefix="aiops-dual-entry-"))
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("AIOPS_")},
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env["AIOPS_GATEWAY_PORT"] = str(port)
    # 管理面配了一把**已知**的密钥：这样"匿名被拒"与"错密钥被拒"才是两件事，
    # 而不是都被"平台未启用"这同一句挡掉。
    env["AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY"] = "verification-management-key"
    # 管理面密钥与 Dify 控制台连接是**同一条**启动条件：只配一半，`validate()` 会拒绝启动
    # （"门开着但每次调用都 502"正是它要防的）。这里指向一个不可达地址 —— 本次要证明的是
    # 鉴权在**调用上游之前**就把请求挡掉，所以它不会被打到。
    env["AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL"] = "http://127.0.0.1:9"
    env["AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY"] = "verification-console-key"
    config = root / "config" / "production.env"
    config.parent.mkdir(mode=0o700)
    config.write_text("# isolated dual-entry management verification\n")
    config.chmod(0o600)

    base = f"http://127.0.0.1:{port}"
    database = root / "data" / "gateway" / "gateway.db"
    out_dir = (repo / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict = {"base": base, "temp_root": str(root), "checks": {}}
    failures: list[str] = []

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
        health = json.loads(body)
        evidence["health"] = health
        print(f"  /health -> {status}")
        if health.get("business_mutations") != "disabled":
            failures.append("business_mutations is not 'disabled'")

        payload = {
            "request_key": "verification000001",
            "tenant_id": "verify-tenant",
            "name": "验证客户－客服与管家",
            "scope": "both",
            "sources": {
                "consumer": {"app_id": "cccccccc-1111-2222-3333-444444444444", "version_no": 1},
                "operator": {"app_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "version_no": 1},
            },
        }
        checks = (
            ("catalogue-anonymous", "GET", f"{MANAGEMENT}/catalogue", None, None, (401, 403)),
            (
                "catalogue-wrong-key",
                "GET",
                f"{MANAGEMENT}/catalogue",
                "not-the-key",
                None,
                (401, 403),
            ),
            (
                "catalogue-right-key-disabled-platform",
                "GET",
                f"{MANAGEMENT}/catalogue",
                "verification-management-key",
                None,
                (403,),
            ),
            (
                "create-anonymous",
                "POST",
                f"{MANAGEMENT}/operations",
                None,
                payload,
                (401, 403),
            ),
            (
                "create-right-key-disabled-platform",
                "POST",
                f"{MANAGEMENT}/operations",
                "verification-management-key",
                payload,
                (403,),
            ),
            ("status-anonymous", "POST", f"{MANAGEMENT}/status", None, {"app_id": "x"}, (401, 403)),
            ("publish-anonymous", "POST", f"{MANAGEMENT}/publish", None, {"app_id": "x"}, (401, 403)),
        )
        for name, method, path, token, body_in, expected in checks:
            status, body = http(f"{base}{path}", token=token, payload=body_in if method == "POST" else None)
            evidence["checks"][name] = {
                "status": status,
                "body": body if len(body) < 500 else body[:500],
            }
            print(f"  {name} -> {status} (expected {expected})")
            if status not in expected:
                failures.append(f"{name}: expected one of {expected}, got {status}")

        created = jobs_count(database)
        evidence["jobs_after_requests"] = created
        print(f"  operator_setup_jobs rows -> {created}")
        if created:
            # 被拒的请求不许留下任何开通任务：否则"拒绝"只是回了一个状态码。
            failures.append(f"{created} setup job row(s) created by refused requests")

        (out_dir / "dual-entry-management.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"  evidence: {out_dir / 'dual-entry-management.json'}")
        if failures:
            print("\nFAILURES:", file=sys.stderr)
            for item in failures:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("all checks passed")
        return 0
    finally:
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
