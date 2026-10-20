"""复跑工具 `live_replay.py` 的判据：不重试、不跟随重定向、不泄漏、证据只留摘要。

全部在本机合成依赖上：回环 HTTP 桩顶替隔离 Gateway，临时状态根顶替栈数据，
`docker`/`systemctl`/`ss` 用替身。**不发任何真实请求**，也不碰生产路径。

每条断言都能单独失败（变异方式写在各自的 docstring 里）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools/m2_close/m3live/live_replay.py"
TOKEN = "handshake-token-3f9a"
ORDER = "SYNTHETIC-ORDER-A"
RUN_ID = "run_" + "a" * 43


def _load():
    spec = importlib.util.spec_from_file_location("live_replay_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Gateway(BaseHTTPRequestHandler):
    """最小的隔离 Gateway 替身：计数、可配置状态码、可配置是否 302。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    status = 200
    body: dict = {}
    redirect_to: str | None = None
    disconnect = False
    redirect_hits = 0

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": payload.decode()}
        )
        if type(self).disconnect:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).redirect_hits += 1
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def gateway():
    _Gateway.requests = []
    _Gateway.status = 200
    _Gateway.body = {
        "run_id": RUN_ID,
        "state": "succeeded",
        "order_no": ORDER,
        "reason": None,
        "agent_version": "abc123",
    }
    _Gateway.redirect_to = None
    _Gateway.disconnect = False
    _Gateway.redirect_hits = 0
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
    order_file = tmp_path / "order"
    order_file.write_text(ORDER + "\n")
    order_file.chmod(0o600)
    return {
        "config": config,
        "state": state,
        "out": tmp_path / "evidence",
        "order_file": order_file,
        "tmp": tmp_path,
    }


def _send_argv(tree, base_url, out=None):
    return [
        "send",
        "--config",
        str(tree["config"]),
        "--state",
        str(tree["state"]),
        "--out",
        str(out or tree["out"]),
        "--order-file",
        str(tree["order_file"]),
        "--base-url",
        base_url,
    ]


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
            json.dumps({"subject": "constructed", "tenant": "TENANT-A"}),
            ORDER,
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


# ---------------------------------------------------------------- 发送：独占与不重试
def test_the_claim_is_taken_before_the_request_and_a_second_send_is_refused(gateway, tree, monkeypatch):
    """先占位再发。

    变异「把 `_claim_send_slot` 挪到请求之后」会让两个并发调用都发出请求；这里用
    第二次调用退出 3 且桩只收到一次来钉住顺序。标记**保留**：结果不确定时不能允许重跑。
    """
    module = _load()
    _init_database(tree["state"], RUN_ID, snapshot={"status": "0"})
    with sqlite3.connect(tree["state"] / "gateway/gateway.db") as connection:
        connection.execute("UPDATE agent_runs SET state = 'succeeded', reason = NULL")
    monkeypatch.setattr(module, "docker_logs", lambda _: ("", 0))
    assert module.main(_send_argv(tree, gateway)) == 0
    assert len(_Gateway.requests) == 1
    assert (tree["out"] / ".send.claim").exists(), "标记必须保留（不重发）"
    assert (tree["out"] / "sent.json").exists()

    assert module.main(_send_argv(tree, gateway)) == 3
    assert len(_Gateway.requests) == 1, "拒绝执行却仍发出了请求"


def test_a_pre_existing_claim_refuses_before_any_request(gateway, tree):
    """目录里已经有标记（上次可能超时/被杀）⇒ 一次请求都不发。"""
    module = _load()
    tree["out"].mkdir(mode=0o700)
    (tree["out"] / ".send.claim").write_text("{}")
    assert module.main(_send_argv(tree, gateway)) == 3
    assert _Gateway.requests == []


def test_a_network_failure_keeps_the_claim_and_does_not_retry(tree):
    """超时/连接失败：非零退出、标记保留、证据里写明 failure。"""
    module = _load()
    # 一个没人监听的端口：连接会被立刻拒绝。
    argv = _send_argv(tree, "http://127.0.0.1:9", out=tree["out"])
    assert module.main(argv) == 1
    assert (tree["out"] / ".send.claim").exists(), "失败后仍不允许重跑"
    receipt = json.loads((tree["out"] / "sent.json").read_text())
    assert receipt["http_status"] is None
    assert receipt["failure"]


def test_a_redirect_is_refused_rather_than_followed(gateway, tree):
    """302 必须变成失败：`urllib` 默认会跟随，并把 `Authorization` 带去新地址。

    变异「去掉 `_NoRedirect`」会让这次调用跟着跳转、把令牌发给另一个主机。
    """
    module = _load()
    _Gateway.redirect_to = gateway + "/elsewhere"
    assert module.main(_send_argv(tree, gateway)) == 1
    assert len(_Gateway.requests) == 1
    assert _Gateway.requests[0]["authorization"] == f"Bearer {TOKEN}"
    assert _Gateway.redirect_hits == 0, "重定向实际收到了带凭据的请求"


def test_disconnect_after_receiving_request_cannot_trigger_a_second_send(gateway, tree):
    module = _load()
    _Gateway.disconnect = True
    argv = _send_argv(tree, gateway)
    assert module.main(argv) == 1
    assert module.main(argv) == 3
    assert len(_Gateway.requests) == 1


def test_concurrent_sends_only_issue_one_request(gateway, tree):
    module = _load()
    _Gateway.status = 502
    barrier = threading.Barrier(2)

    def send():
        barrier.wait(timeout=5)
        return module.main(_send_argv(tree, gateway))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: send(), range(2)))
    assert sorted(outcomes) == [1, 3]
    assert len(_Gateway.requests) == 1


def test_success_receipt_cannot_override_a_failed_database_record(gateway, tree):
    module = _load()
    _init_database(tree["state"], RUN_ID, snapshot={"status": "0"})
    assert module.main(_send_argv(tree, gateway)) == 1


def test_legacy_sent_marker_still_prevents_sending(gateway, tree):
    tree["out"].mkdir(mode=0o700)
    (tree["out"] / "sent.json").write_text("{}")
    assert _load().main(_send_argv(tree, gateway)) == 3
    assert not _Gateway.requests


@pytest.mark.parametrize(
    "base_url",
    [
        "https://supplier.example/v1",
        "http://169.254.169.254",
        "http://user:pass@127.0.0.1:8794",
        "http://127.0.0.1:8794/prefix",
        "file:///etc/passwd",
    ],
)
def test_a_non_loopback_or_decorated_base_url_is_refused(tree, base_url):
    """令牌只发给本机栈：回环以外、带 userinfo、带路径、非 http(s) 一律拒绝，且不发请求。"""
    module = _load()
    _Gateway.requests = []
    assert module.main(_send_argv(tree, base_url)) == 3
    assert _Gateway.requests == []


def test_the_order_number_comes_from_a_private_file_not_the_command_line(tree):
    """命令行里的订单号进 shell 历史与 `ps`；工具只接受 `--order-file`。"""
    source = TOOL.read_text(encoding="utf-8")
    assert "--order-no" not in source, "订单号又回到命令行参数了"
    parser = _load().build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["send", "--out", "x", "--order-no", ORDER])


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_an_unsafe_order_file_is_refused(tree, mode):
    module = _load()
    tree["order_file"].chmod(mode)
    with pytest.raises(module.Refused):
        module.read_order_no(tree["order_file"])


def test_a_relative_or_absolute_path_in_the_order_file_is_refused(tree):
    module = _load()
    tree["order_file"].write_text("../../etc/passwd\n")
    with pytest.raises(module.Refused):
        module.read_order_no(tree["order_file"])


# ---------------------------------------------------------------- 证据：只留摘要
def test_collect_keeps_summaries_not_business_bodies_or_personal_identifiers(tree):
    """记录以**白名单**进证据：订单号、scope_json、快照正文都不出现。"""
    module = _load()
    _init_database(tree["state"], RUN_ID, snapshot={"order_no": ORDER, "soc": 55})
    payload = module.collect(tree["state"], RUN_ID, gateway_container="g", node_container="n")
    record = payload["record"]
    assert record["run_id"] == RUN_ID
    assert record["has_snapshot"] is True
    assert record["snapshot_keys"] == ["order_no", "soc"]
    assert "scope_json" not in record
    assert "order_no" not in record
    assert "_snapshot" not in record
    text = json.dumps(payload)
    assert "TENANT-A" not in text, "构造身份的租户进了证据"
    assert ORDER not in text, "订单号（个人业务标识）进了证据"
    assert '\\"order_no\\": ' + '"' + ORDER + '"' not in text
    # 快照值不进证据：只留键名与摘要。
    assert record["snapshot_keys"] == ["order_no", "soc"]
    assert '"soc": 55' not in json.dumps(record)


def test_logs_are_summarised_into_counts_not_copied(tree):
    """日志行可能带业务正文：证据只留「出现几次、出现过哪几类标记」。"""
    module = _load()
    text = (
        f"2026-10-20 dispatch sid={RUN_ID} order={ORDER}\n"
        f"2026-10-20 claim ok run_id={RUN_ID}\n"
        "2026-10-20 unrelated line\n"
    )
    summary = module.summarise_run_log(text, RUN_ID)
    assert summary["lines_matching_run"] == 2
    assert summary["markers"]["sid="] == 1
    assert summary["markers"]["claim"] == 1
    assert ORDER not in json.dumps(summary)


def test_the_model_request_count_is_declared_unobserved(tree):
    module = _load()
    _init_database(tree["state"], RUN_ID)
    payload = module.collect(tree["state"], RUN_ID, gateway_container="g", node_container="n")
    assert payload["model_request_count"]["observed"] is False
    assert payload["model_request_count"]["value"] is None


def test_a_missing_run_is_reported_rather_than_invented(tree):
    module = _load()
    _init_database(tree["state"], RUN_ID)
    payload = module.collect(tree["state"], "run_" + "c" * 43, gateway_container="g", node_container="n")
    assert payload["record"] is None
    assert payload["database_error"] is None


def test_corrupt_database_is_an_observation_failure(tree):
    module = _load()
    (tree["state"] / "gateway/gateway.db").write_text("corrupt")
    payload = module.collect(tree["state"], RUN_ID, gateway_container="g", node_container="n")
    assert payload["record"] is None
    assert payload["database_error"]


def test_missing_logs_make_a_successful_run_incomplete(gateway, tree, monkeypatch):
    module = _load()
    _init_database(tree["state"], RUN_ID, snapshot={"status": "0"})
    with sqlite3.connect(tree["state"] / "gateway/gateway.db") as connection:
        connection.execute("UPDATE agent_runs SET state = 'succeeded'")
    monkeypatch.setattr(module, "docker_logs", lambda _: ("unavailable", 1))
    assert module.main(_send_argv(tree, gateway)) == 1


def test_hardlinked_evidence_target_is_not_truncated(tmp_path):
    module = _load()
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    original = tmp_path / "original"
    original.write_text("keep")
    os.link(original, out / "evidence.json")
    with pytest.raises(module.Refused):
        module._write_evidence(out, "evidence.json", {"new": True}, token="")
    assert original.read_text() == "keep"


@pytest.mark.parametrize("run_id", ["../etc", "run_/../../x", "run_short", "run_" + "a" * 200])
def test_a_bad_run_id_is_refused_before_it_can_be_used_as_a_path(tree, run_id):
    module = _load()
    with pytest.raises(module.Refused):
        module.read_run(tree["state"], run_id)


def test_evidence_is_0600_and_refuses_a_symlinked_target(tmp_path):
    module = _load()
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.json"
    (out / "x.json").symlink_to(target)
    with pytest.raises(module.Refused):
        module._write_evidence(out, "x.json", {"a": 1}, token="")
    assert not target.exists(), "顺着符号链接写了盘"

    path = module._write_evidence(out, "y.json", {"a": 1}, token="")
    assert path.stat().st_mode & 0o777 == 0o600


def test_evidence_never_contains_the_token_or_a_supplier_key(tmp_path):
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


# ---------------------------------------------------------------- 清理：观测不完整必须非零
def _facts(**overrides):
    base = {
        "captured_at": "x",
        "unit_active_enter": "2026-10-20 06:32:09 UTC",
        "unit_active_state": "active",
        "config_sha256": {"gateway.env": "a" * 64},
        "agent_runs_rows": 0,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("failed_command", ["docker", "ss", None])
def test_cleanup_fails_when_docker_cannot_be_read(monkeypatch, tree, failed_command):
    """`docker ps` 非零**不是**「没有容器」：观测失败必须非零。"""
    module = _load()

    def command(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 17 if argv[0] == failed_command else 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", command)
    monkeypatch.setattr(module, "production_facts", lambda: _facts())
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", _write(tree, "b.json", _facts())]
    )
    assert module.cmd_cleanup(args) == (1 if failed_command else 0)
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    if failed_command:
        assert any(failed_command in item for item in report["problems"])
    else:
        assert report["containers"] == report["listeners_on_port"] == []
        assert report["problems"] == []


def test_cleanup_fails_when_the_port_cannot_be_observed(monkeypatch, tree):
    module = _load()

    def _boom(port):
        raise module.Incomplete("ss")

    monkeypatch.setattr(module, "stack_containers", lambda project: [])
    monkeypatch.setattr(module, "listening_on", _boom)
    monkeypatch.setattr(module, "production_facts", lambda: _facts())
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", _write(tree, "b.json", _facts())]
    )
    assert module.cmd_cleanup(args) == 1


def test_cleanup_fails_when_a_production_fact_is_missing(monkeypatch, tree):
    """基线里缺一项：**不能**跳过后算通过。"""
    module = _load()
    monkeypatch.setattr(module, "stack_containers", lambda project: [])
    monkeypatch.setattr(module, "listening_on", lambda port: [])
    monkeypatch.setattr(module, "production_facts", lambda: _facts())
    incomplete = _facts()
    incomplete.pop("config_sha256")
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", _write(tree, "b.json", incomplete)]
    )
    assert module.cmd_cleanup(args) == 1
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    assert report["production"]["compared"] is False


def test_cleanup_passes_with_a_complete_baseline_and_no_leftovers(monkeypatch, tree):
    """正对照：观测齐全、容器与端口都空、生产面一致 ⇒ 0。"""
    module = _load()
    monkeypatch.setattr(module, "stack_containers", lambda project: [])
    monkeypatch.setattr(module, "listening_on", lambda port: [])
    monkeypatch.setattr(module, "production_facts", lambda: _facts())
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", _write(tree, "b.json", _facts())]
    )
    assert module.cmd_cleanup(args) == 0
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    assert report["production"]["compared"] is True
    assert report["problems"] == []


def test_cleanup_reports_production_drift(monkeypatch, tree):
    module = _load()
    monkeypatch.setattr(module, "stack_containers", lambda project: [])
    monkeypatch.setattr(module, "listening_on", lambda port: [])
    monkeypatch.setattr(module, "production_facts", lambda: _facts(agent_runs_rows=3))
    args = module.build_parser().parse_args(
        ["cleanup", "--out", str(tree["out"]), "--prod-baseline", _write(tree, "b.json", _facts())]
    )
    assert module.cmd_cleanup(args) == 1
    report = json.loads((tree["out"] / "cleanup.json").read_text())
    assert any("生产面变化" in item for item in report["problems"])


def test_the_production_baseline_is_required(tree):
    """没有基线，「生产没变」这句话没有出处 —— 参数层面就不给这个形状。"""
    parser = _load().build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cleanup", "--out", str(tree["out"])])


def test_config_facts_are_content_digests_not_mtime(monkeypatch, tree):
    """配置对比用内容 sha256：`cp -p` 与同一秒内的写入都会让 mtime 说谎。"""
    module = _load()
    prod = tree["tmp"] / "prod"
    prod.mkdir()
    (prod / "gateway.env").write_text("A=1\n")
    monkeypatch.setattr(module, "PROD_CONFIG_DIR", prod)
    first = module._config_digests()
    (prod / "gateway.env").write_text("A=2\n")
    os.utime(prod / "gateway.env", (1, 1))
    second = module._config_digests()
    assert first != second, "内容变了但摘要没变"


def test_production_facts_refuses_to_return_partial_data(monkeypatch, tree):
    """任何一项拿不到都抛 `Incomplete`，而不是返回一个带 None 的 dict。"""
    module = _load()

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(module, "_run", lambda argv, timeout=20.0: _Failed())
    with pytest.raises(module.Incomplete):
        module.production_facts()


def _write(tree, name, payload) -> str:
    path = tree["tmp"] / name
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return str(path)


def test_the_handshake_token_never_comes_from_the_command_line():
    """源码级钉子：身份只从 0600 文件读，没有 `--token`。"""
    source = TOOL.read_text(encoding="utf-8")
    assert "--token" not in source
    assert "AIOPS_M2_LOCAL_TOKEN" in source
    assert "identity.env" in source
    assert not os.environ.get("AIOPS_M2_LOCAL_TOKEN"), "测试环境里不该有真实握手令牌"
