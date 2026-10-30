#!/usr/bin/env python3
"""M3-GATEWAY-01 的**真实 HTTP** 验收：一次性数据根 + 真 `create_gateway_app` + 回环替身。

为什么要这一层：单测证的是「路由函数在该判据下的行为」，而 `/health` 200 只证明端口在听。
本脚本让**真的进程**跑起来，用真的 HTTP 打委派入口，由平台替身扮演节点回呼领取入口，
覆盖四种组合并直接读数据库核对终态：

| 组合 | 节点回传 | 期望 |
|---|---|---|
| 一致 | 正确身份 | 领取 200，记录 `matched`，回执 `agent_version` 逐字等于清单身份 |
| 缺失 | 不带该头 | 领取 403 `AGENT_RUN_EXEC_CONFIG_DRIFT`，记录 `failed`/`exec_config_drift`，无快照 |
| 错摘要 | 另一个 sha256 | 同上，且 `observed_agent_version` 落库供审计 |
| 错误节点 | 兄弟节点凭据 | 403 `AGENT_RUN_WRONG_NODE`，**不因本片新增的对照改变既有拒绝理由** |

被替换的只有外部依赖（调用者身份、订单授权源、CPMS、编排平台）；**委派与领取路由、
`AgentRunStore` 生命周期、清单加载与对照全部是本仓真实代码**。不接真实供应商、不访问 41。

    uv run python tools/experiments/m2_close/m3node/verify_gateway_exec_config.py \
        --out /tmp/exec-config-http.json

退出码 0=PASS，1=有判据失败，2=未取证（前置缺失）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SAMPLES = Path(__file__).resolve().parent / "manifest-samples"
TOKEN = "exec-config-real-token"
NODE_TOKEN = "exec-config-real-node-token"
SIBLING_TOKEN = "exec-config-real-sibling-token"
TENANT = "TENANT-A"
C_USER = "USER-A"


def http(
    url: str,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 连不上也要成为一条可观察结果
        return 0, f"{type(exc).__name__}: {exc}"


class CpmsHandler(BaseHTTPRequestHandler):
    """与真实 CPMS 同形的桩：``code/data`` 信封，订单存在即返回带敏感字段的正文。"""

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        CpmsHandler.calls += 1
        order_no = self.path.rsplit("/", 1)[-1]
        payload = {
            "code": 0,
            "msg": "success",
            "data": {"orderNo": order_no, "status": "0", "plateNo": "DO-NOT-LEAK", "userId": "DO-NOT-LEAK"},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SnailHandler(BaseHTTPRequestHandler):
    """编排平台替身：收到派发后**扮演节点**回呼领取入口，再把结果交回平台。

    这是真实时序的替身，不是被测逻辑的替身 —— 领取判据全在 Gateway 里跑。
    """

    gateway = ""
    state: dict[str, Any] = {}

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        # 平台只知道 sid（= run_id），它与「记录已签发」之间没有顺序保证：
        # 等调度侧把 run_id 填进来再回呼，而不是拿空串去猜。
        deadline = time.monotonic() + 30
        while not SnailHandler.state.get("run_id") and time.monotonic() < deadline:
            time.sleep(0.02)
        run_id = SnailHandler.state.get("run_id") or ""
        headers = SnailHandler.state["claim_headers"]
        status, text = http(
            f"{SnailHandler.gateway}/v1/internal/charging-status/claims/{run_id}",
            method="POST",
            token=SnailHandler.state["claim_token"],
            body={},
            headers=headers,
        )
        SnailHandler.state["claim_result"] = {"status": status, "body": text}
        if status == 200:
            # 真实节点在整轮结束时还会发一次完成通知（M3-NODE-LINK-01）。本替身扮演的是
            # 节点，所以它也要发 —— 否则启用清单的 run 会正确地停在 running，回执
            # `agent_version` 就永远是 null，这里测的就不再是「已核对」。
            completion, completion_body = http(
                f"{SnailHandler.gateway}/v1/internal/charging-status/completions/{run_id}",
                method="POST",
                token=SnailHandler.state["claim_token"],
                body={"status": "completed", "reason": "model_stop"},
                headers=headers,
            )
            SnailHandler.state["completion_result"] = {
                "status": completion,
                "body": completion_body,
            }
            retries = {}
            for label, body in (
                ("same", {"status": "completed", "reason": "model_stop"}),
                ("missing", {"status": "completed"}),
                ("null", {"status": "completed", "reason": None}),
            ):
                code, _ = http(
                    f"{SnailHandler.gateway}/v1/internal/charging-status/completions/{run_id}",
                    method="POST",
                    token=SnailHandler.state["claim_token"],
                    body=body,
                    headers=headers,
                )
                retries[label] = code
            SnailHandler.state["completion_retries"] = retries
            if SnailHandler.state.get("after_completion") == "transfer":
                with sqlite3.connect(SnailHandler.state["orders_db"]) as connection:
                    connection.execute("UPDATE synthetic_orders SET user_id='USER-B'")
        body = json.dumps({"status": 1, "data": {"content": "已查询"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _rows(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs")]
    finally:
        connection.close()


def _case(
    root: Path, *, label: str, claim_token: str, header_mode: str, want: str, after_completion: str = ""
):
    """在一个全新的数据根上跑一次完整的委派 → 平台 → 节点领取 → 回执。

    ``header_mode``：``identity`` 回传清单真实身份、``wrong`` 回传另一个摘要、
    ``none`` 不带该头。头在**算出身份之后**才构造 —— 否则要么写死一个会过期的摘要，
    要么先跑一次猜身份（后者会污染「缺省不等于一致」这条判据）。
    """
    case_root = root / label
    (case_root / "config").mkdir(parents=True)
    (case_root / "data").mkdir(parents=True)
    settings_file = case_root / "config/production.env"
    settings_file.write_text("# synthetic\n")
    settings_file.chmod(0o600)

    manifest = case_root / "exec-manifest.json"
    shutil.copyfile(SAMPLES / "accept-normal.json", manifest)
    manifest.chmod(0o600)
    identity = hashlib.sha256(manifest.read_bytes()).hexdigest()

    agent_runs = case_root / "config/agent-runs.json"
    agent_runs.write_text(
        json.dumps(
            {
                "environment": "env-local",
                "snail_base_url": f"http://127.0.0.1:{SnailHandler.server_port}",
                "snail_app_id": "exec-config-check",
                "snail_app_token": "unused",
                "snail_agent_id": 1,
                "snail_open_id": "open-id",
                "expected_node_id": "node-local",
                "nodes": [
                    {
                        "node_id": "node-local",
                        "environment": "env-local",
                        "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                    },
                    {
                        "node_id": "node-local-2",
                        "environment": "env-local",
                        "token_sha256": hashlib.sha256(SIBLING_TOKEN.encode()).hexdigest(),
                    },
                ],
            }
        )
    )
    agent_runs.chmod(0o600)

    orders = case_root / "data/orders.db"
    connection = sqlite3.connect(orders)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS synthetic_orders "
        "(order_no TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL)"
    )
    connection.execute("INSERT OR REPLACE INTO synthetic_orders VALUES ('ORDER-A', ?, ?)", (C_USER, TENANT))
    connection.commit()
    connection.close()

    port = _free_port()
    env = {
        **os.environ,
        "AIOPS_DATA_HOME": str(case_root / "data"),
        "AIOPS_CONFIG_HOME": str(case_root / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(case_root / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(settings_file),
        "AIOPS_GATEWAY_DATABASE_FILE": str(case_root / "data/gateway.db"),
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(port),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"http://127.0.0.1:{CpmsHandler.server_port}/charging-pile",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE": str(agent_runs),
        "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE": str(manifest),
        "AIOPS_M2_LOCAL_TOKEN": TOKEN,
        "AIOPS_M2_ORDERS_DB": str(orders),
        "AIOPS_M2_TENANT": TENANT,
        "AIOPS_M2_C_USER": C_USER,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    base = f"http://127.0.0.1:{port}"
    claim_headers = {
        "identity": {"X-AI-Ops-Exec-Config": identity},
        "wrong": {"X-AI-Ops-Exec-Config": "f" * 64},
        "none": {},
    }[header_mode]
    SnailHandler.state = {
        "claim_token": claim_token,
        "claim_headers": claim_headers,
        "identity": identity,
        "after_completion": after_completion,
        "orders_db": str(orders),
    }
    CpmsHandler.calls = 0
    process = subprocess.Popen(
        [sys.executable, str(REPO / "tools/experiments/m2_close/gateway_launcher.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if http(f"{base}/health", timeout=2.0)[0] == 200:
                break
            time.sleep(0.3)
        else:
            return {"label": label, "error": "gateway did not become ready", "ok": False}

        SnailHandler.gateway = base
        # 委派请求本身会阻塞到平台替身把节点那一跳走完，所以 run_id 由数据库在派发后取。
        result: dict[str, Any] = {"label": label}
        runner = threading.Thread(target=lambda: result.update(_delegate(base)))
        runner.start()
        # 等记录出现，再把它的 run_id 交给平台替身。
        run_id = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = _rows(case_root / "data/gateway.db")
            if rows:
                run_id = str(rows[0]["run_id"])
                if run_id:
                    SnailHandler.state["run_id"] = run_id
                    break
            time.sleep(0.05)
        runner.join(timeout=60)
        rows = _rows(case_root / "data/gateway.db")
        row = rows[0] if rows else {}
        claim = SnailHandler.state.get("claim_result") or {}
        return {
            "label": label,
            "ok": True,
            "want": want,
            "identity": identity,
            "delegated": result,
            "claim_status": claim.get("status"),
            "claim_body": json.loads(claim.get("body") or "{}"),
            "completion_retries": SnailHandler.state.get("completion_retries", {}),
            "cpms_calls": CpmsHandler.calls,
            "snapshot": json.loads(row["snapshot_json"]) if row.get("snapshot_json") else None,
            "row": {
                "state": row.get("state"),
                "reason": row.get("reason"),
                "expected_agent_version": row.get("expected_agent_version"),
                "observed_agent_version": row.get("observed_agent_version"),
                "exec_config_state": row.get("exec_config_state"),
                "has_snapshot": bool(row.get("snapshot_json")),
            },
        }
    finally:
        _stop(process)
        shutil.rmtree(case_root, ignore_errors=True)


def _delegate(base: str) -> dict[str, Any]:
    status, text = http(
        f"{base}/v1/orders/ORDER-A/charging-status/delegated",
        method="POST",
        token=TOKEN,
        body={},
        headers={"X-Business-Entry": "consumer"},
    )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw": text[:200]}
    return {"status": status, "body": parsed}


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "kind": "m3_gateway_exec_config_http",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "synthetic_data": True,
        "real_company_services": False,
        "substituted": {
            "caller_identity": "fixed local token -> fixed consumer ScopeContext",
            "order_authorization": "synthetic_orders table in the same SQLite file",
            "cpms": "loopback stub, same code/data envelope",
            "orchestration_platform": "loopback stub that performs the node's claim call",
        },
        "real": [
            "delegated route",
            "internal claim route",
            "agent run store / lifecycle",
            "execution manifest load + identity comparison",
            "receipt assembly",
        ],
        "checks": [],
    }

    global CpmsHandler, SnailHandler
    cpms = _serve(CpmsHandler)
    snail = _serve(SnailHandler)
    CpmsHandler.server_port = cpms.server_port  # type: ignore[attr-defined]
    SnailHandler.server_port = snail.server_port  # type: ignore[attr-defined]
    root = Path(tempfile.mkdtemp(prefix="m3-exec-config-"))
    report["temp_root"] = str(root)

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        cases: dict[str, dict[str, Any]] = {}

        matched = _case(root, label="matched", claim_token=NODE_TOKEN, header_mode="identity", want="matched")
        row = matched.get("row") or {}
        check(
            "一致身份：领取 200",
            matched.get("claim_status") == 200,
            f"status={matched.get('claim_status')} {matched.get('claim_body')}",
        )
        check(
            "一致身份：记录 matched 且期望=实际",
            row.get("exec_config_state") == "matched"
            and row.get("expected_agent_version") == matched.get("identity")
            and row.get("observed_agent_version") == matched.get("identity"),
            str(row),
        )
        body = (matched.get("delegated") or {}).get("body") or {}
        snapshot = body.get("charging_status")
        check(
            "消费者同 run 快照",
            snapshot == matched.get("snapshot") and isinstance(snapshot, dict),
            str(snapshot),
        )
        check(
            "消费者快照摘要",
            isinstance(snapshot, dict)
            and hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            == body.get("result_sha256"),
            str(body.get("result_sha256")),
        )
        check("消费者不二次读取业务", matched.get("cpms_calls") == 1, str(matched.get("cpms_calls")))
        for label, expected in (("same", 200), ("missing", 409), ("null", 409)):
            actual = matched.get("completion_retries", {}).get(label)
            check(f"成功原因重试 {label}", actual == expected, f"HTTP {actual}, expected {expected}")
        check(
            "一致身份：回执 agent_version 取自已核对记录",
            body.get("agent_version") == matched.get("identity") and body.get("agent_version_gap") is None,
            str({k: body.get(k) for k in ("agent_version", "agent_version_gap")}),
        )

        transferred = _case(
            root,
            label="transferred",
            claim_token=NODE_TOKEN,
            header_mode="identity",
            want="refused",
            after_completion="transfer",
        )
        transferred_result = transferred.get("delegated") or {}
        check(
            "领取后转移订单拒绝消费者快照",
            transferred_result.get("status") == 404
            and (transferred_result.get("body") or {}).get("error", {}).get("code") == "ORDER_NOT_FOUND"
            and "charging_status" not in (transferred_result.get("body") or {}),
            str(transferred_result),
        )
        check("拒绝后不多读业务", transferred.get("cpms_calls") == 1, str(transferred.get("cpms_calls")))

        missing = _case(root, label="missing", claim_token=NODE_TOKEN, header_mode="none", want="missing")
        row = missing.get("row") or {}
        check("缺失头：领取 403", missing.get("claim_status") == 403, f"status={missing.get('claim_status')}")
        check(
            "缺失头：错误码是执行配置漂移",
            ((missing.get("claim_body") or {}).get("error") or {}).get("code")
            == "AGENT_RUN_EXEC_CONFIG_DRIFT",
            str(missing.get("claim_body")),
        )
        check(
            "缺失头：记录落终态且无快照",
            row.get("state") == "failed"
            and row.get("reason") == "exec_config_drift"
            and not row.get("has_snapshot"),
            str(row),
        )

        wrong = _case(root, label="wrong-digest", claim_token=NODE_TOKEN, header_mode="wrong", want="drift")
        row = wrong.get("row") or {}
        check("错摘要：领取 403", wrong.get("claim_status") == 403, f"status={wrong.get('claim_status')}")
        check(
            "错摘要：实际值落库供审计、状态 mismatched",
            row.get("observed_agent_version") == "f" * 64 and row.get("exec_config_state") == "mismatched",
            str(row),
        )
        check(
            "错摘要：不放行任何业务正文",
            "order_no" not in ((wrong.get("claim_body") or {}).get("raw") or "")
            and "order_no" not in json.dumps(wrong.get("claim_body") or {}),
            str(wrong.get("claim_body")),
        )

        sibling = _case(
            root, label="wrong-node", claim_token=SIBLING_TOKEN, header_mode="none", want="wrong_node"
        )
        row = sibling.get("row") or {}
        check(
            "错误节点：既有拒绝理由未变",
            ((sibling.get("claim_body") or {}).get("error") or {}).get("code") == "AGENT_RUN_WRONG_NODE",
            str(sibling.get("claim_body")),
        )
        check(
            "错误节点：他人记录不被改写",
            row.get("state") in {"issued", "running"}
            and row.get("observed_agent_version") is None
            and row.get("reason") is None,
            str(row),
        )

        report["cases"] = cases | {"missing": missing, "wrong-digest": wrong, "wrong-node": sibling}
    except Exception as exc:  # noqa: BLE001 - 报告记录失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        check("验证未完成", False, report["error"])
    finally:
        cpms.shutdown()
        snail.shutdown()
        shutil.rmtree(root, ignore_errors=True)
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text)
        else:
            print(text)

    print(f"{report['status']}", file=sys.stderr)
    for item in report["checks"]:
        if not item["ok"]:
            print(f"  - {item['name']}: {item['detail']}", file=sys.stderr)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
