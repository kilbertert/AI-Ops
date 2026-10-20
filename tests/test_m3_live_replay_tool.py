"""复跑工具 `live_replay.py` 的判据：不重试、不泄漏、证据按 run 绑定。

全部在本机合成依赖上：一个回环 HTTP 桩顶替隔离 Gateway，一个临时状态根顶替栈数据，
`docker`/`systemctl`/`ss` 用替身。**不发任何真实请求**，也不碰生产路径。

三条判据各自可以单独失败（变异见注释）：

1. 同一输出目录第二次 `send` 退出 3，且**桩只收到一次**请求；
2. 失败响应非零退出、证据落盘、仍然不重试；
3. 令牌与 `sk-` 形状的字符串不出现在任何证据里。
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools/m2_close/m3live/live_replay.py"
TOKEN = "handshake-token-3f9a"


def _load():
    spec = importlib.util.spec_from_file_location("live_replay_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Gateway(BaseHTTPRequestHandler):
    """最小的隔离 Gateway 替身：计数、可配置状态码。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    status = 200
    body: dict = {}

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": payload.decode()}
        )
        body = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def gateway():
    _Gateway.requests = []
    _Gateway.status = 200
    _Gateway.body = {"run_id": "run_" + "a" * 43, "state": "succeeded"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tree(tmp_path):
    config = tmp_path / "etc"
    state = tmp_path / "state"
    (config / "gateway").mkdir(parents=True)
    (state / "gateway").mkdir(parents=True)
    identity = config / "gateway/identity.env"
    identity.write_text(f"AIOPS_M2_LOCAL_TOKEN={TOKEN}\nAIOPS_M2_TENANT=TENANT-A\nAIOPS_M2_C_USER=USER-A\n")
    identity.chmod(0o600)
    return {"config": config, "state": state, "out": tmp_path / "evidence"}


def _init_database(state: Path, run_id: str, *, snapshot: dict | None = None) -> None:
    import sqlite3

    connection = sqlite3.connect(state / "gateway/gateway.db")
    connection.executescript(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY, scope_fingerprint TEXT NOT NULL, scope_json TEXT NOT NULL,
            order_no TEXT NOT NULL, operation TEXT NOT NULL, environment TEXT NOT NULL,
            app_id TEXT NOT NULL, agent_id TEXT NOT NULL, expected_node_id TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT, snapshot_json TEXT, actual_node_id TEXT,
            node_build TEXT, gateway_version TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT,
            expected_agent_version TEXT, observed_agent_version TEXT, exec_config_state TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "fp",
            json.dumps({"subject": "constructed"}),
            "ORDER-A",
            "charging_status_snapshot",
            "live-trial-test",
            "app",
            "1",
            "node-41",
            "failed",
            "model_failed",
            json.dumps(snapshot) if snapshot is not None else None,
            "node-41",
            "build",
            "0.1.0+test",
            "2026-10-20T00:00:00+00:00",
            "2026-10-20T00:03:00+00:00",
            "2026-10-20T00:00:01+00:00",
            "2026-10-20T00:00:02+00:00",
            "abc123",
            "abc123",
            "matched",
        ),
    )
    connection.commit()
    connection.close()


def test_send_refuses_a_second_send_and_only_one_request_reaches_the_gateway(gateway, tree):
    """不重试：同一输出目录只发一次。变异「去掉 sent.json 检查」会让桩收到两次。"""
    module = _load()
    argv = [
        "send",
        "--config",
        str(tree["config"]),
        "--state",
        str(tree["state"]),
        "--out",
        str(tree["out"]),
        "--order-no",
        "ORDER-A",
        "--base-url",
        gateway,
    ]
    assert module.main(argv) == 0
    assert len(_Gateway.requests) == 1
    sent = json.loads((tree["out"] / "sent.json").read_text())
    assert sent["http_status"] == 200
    assert sent["identity"]["note"].startswith("调用者身份是构造的")

    # 第二次：退出 3（拒绝执行），且没有第二个请求到达。
    assert module.main(argv) == 3
    assert len(_Gateway.requests) == 1, "拒绝执行却仍发出了请求"


def test_a_failed_dispatch_exits_non_zero_and_does_not_retry(gateway, tree):
    module = _load()
    _Gateway.status = 502
    _Gateway.body = {"error": {"code": "AGENT_RUN_NO_RESULT", "message": "agent run produced no result"}}
    argv = [
        "send",
        "--config",
        str(tree["config"]),
        "--state",
        str(tree["state"]),
        "--out",
        str(tree["out"]),
        "--order-no",
        "ORDER-A",
        "--base-url",
        gateway,
    ]
    assert module.main(argv) == 1
    assert len(_Gateway.requests) == 1, "失败被重试了"
    assert (tree["out"] / "sent.json").exists(), "失败也要留下证据"


def test_collect_binds_evidence_to_one_run_and_drops_the_business_body(tree):
    """按 run 采证：快照只留摘要与键集合；正文不写进证据。"""
    module = _load()
    run_id = "run_" + "b" * 43
    _init_database(tree["state"], run_id, snapshot={"order_no": "ORDER-A", "soc": 55})
    payload = module.collect(tree["state"], run_id, gateway_container="g", node_container="n")
    record = payload["record"]
    assert record["run_id"] == run_id
    assert record["has_snapshot"] is True
    assert record["snapshot_keys"] == ["order_no", "soc"]
    assert "scope_json" not in record
    # 观测缺口写在证据里，而不是只在文档里
    assert payload["model_request_count"]["observed"] is False
    assert payload["model_request_count"]["value"] is None


def test_a_missing_run_is_reported_rather_than_invented(tree):
    module = _load()
    payload = module.collect(tree["state"], "run_" + "c" * 43, gateway_container="g", node_container="n")
    assert payload["record"] is None


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_an_permissive_identity_file_is_refused(tree, mode):
    module = _load()
    identity = tree["config"] / "gateway/identity.env"
    identity.chmod(mode)
    with pytest.raises(module.Refused):
        module.read_identity(tree["config"])


def test_a_symlinked_identity_file_is_refused(tree):
    module = _load()
    identity = tree["config"] / "gateway/identity.env"
    real = tree["config"] / "gateway/real.env"
    identity.rename(real)
    real.chmod(0o600)
    identity.symlink_to(real)
    with pytest.raises(module.Refused):
        module.read_identity(tree["config"])


def test_evidence_never_contains_the_token_or_a_supplier_key(tmp_path):
    """写盘前断言：令牌与 `sk-…` 形状都不允许出现在证据里。"""
    module = _load()
    payload = {
        "body": {"error": "authorization: Bearer abc123 provider said [sk-0123456789abcdef] 余额不足"},
        "token_echo": TOKEN,
    }
    path = module._write_evidence(tmp_path / "out", "x.json", payload, token=TOKEN)
    text = path.read_text()
    assert TOKEN not in text
    assert "sk-0123456789abcdef" not in text
    assert "***" in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_cleanup_reports_leftover_containers_and_port(monkeypatch, tree):
    """清理判据：容器还在或端口还在监听都必须非零。"""
    module = _load()
    monkeypatch.setattr(module, "stack_containers", lambda project: ["m2live03-gateway"])
    monkeypatch.setattr(module, "listening_on", lambda port: ["LISTEN 0 4096 127.0.0.1:8794"])
    args = module.build_parser().parse_args(["cleanup", "--out", str(tree["out"])])
    args.func = module.cmd_cleanup
    assert module.cmd_cleanup(args) == 1
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    assert any("容器" in item for item in report["problems"])
    assert report["production"]["compared"] is False


def test_cleanup_compares_production_facts_when_a_baseline_is_given(monkeypatch, tree):
    module = _load()
    baseline = tree["out"].parent / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "unit_active_enter": "2026-10-20 06:32:09 UTC",
                "unit_active_state": "active",
                "config_files": {"gateway.env": [10, 100]},
                "agent_runs_rows": 0,
            }
        )
    )
    monkeypatch.setattr(module, "stack_containers", lambda project: [])
    monkeypatch.setattr(module, "listening_on", lambda port: [])
    monkeypatch.setattr(
        module,
        "production_facts",
        lambda: {
            "captured_at": "x",
            "unit_active_enter": "2026-10-20 06:32:09 UTC",
            "unit_active_state": "active",
            "config_files": {"gateway.env": [10, 100]},
            "agent_runs_rows": 0,
        },
    )
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", str(baseline)]
    )
    assert module.cmd_cleanup(args) == 0
    # 反面对照：只要生产面有一项变了，就必须非零并写进 problems。
    monkeypatch.setattr(
        module,
        "production_facts",
        lambda: {
            "captured_at": "x",
            "unit_active_enter": "2026-10-21 00:00:00 UTC",
            "unit_active_state": "active",
            "config_files": {"gateway.env": [10, 100]},
            "agent_runs_rows": 0,
        },
    )
    assert module.cmd_cleanup(args) == 1
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    assert any("生产面变化" in item for item in report["problems"])


def test_the_handshake_token_never_comes_from_the_command_line():
    """源码级钉子：身份只从 0600 文件读，没有 `--token`。

    一条命令行参数会同时进 shell 历史与 `ps`；这正是任务书禁止的形状。
    """
    source = TOOL.read_text(encoding="utf-8")
    assert "--token" not in source
    assert "AIOPS_M2_LOCAL_TOKEN" in source
    assert "identity.env" in source
    assert not os.environ.get("AIOPS_M2_LOCAL_TOKEN"), "测试环境里不该有真实握手令牌"
