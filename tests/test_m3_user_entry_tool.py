"""M3 用户入口工具：零网络预检、凭据隔离、一次派发、记录与日志核对。

链路测试使用真实 HTTP、create_gateway_app 与 RedisThirdSessionResolver；
Redis、订单授权器、CPMS、平台/节点回呼是替身，不证明正常登录或 App 业务验收。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools/m2_close/m3live/user_entry.py"

SERVICE_TOKEN = "service-token-9c31"
SESSION = "session-abcdef0123456789"
TENANT = "TENANT-A"
C_OWNER = "USER-A"
C_OTHER = "USER-B"
ORDER_OWN = "ORDER-A"
ORDER_OTHER = "ORDER-B"
RUN_ID = "run_" + "b" * 43
NODE_TOKEN = "node-token-alpha-0123456789"


def _load():
    spec = importlib.util.spec_from_file_location("user_entry_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 输入与工具规矩
def test_the_session_and_the_service_token_are_never_command_line_arguments():
    """凭据只接受文件路径，不接受会进入 shell 历史与 ps 的明文参数。"""
    parser = _load().build_parser()
    subactions = [action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"]
    assert subactions, "解析器结构变了，这条判据需要跟着改"
    names = sorted(
        {
            option
            for action in subactions[0].choices.values()
            for option_action in action._actions
            for option in option_action.option_strings
        }
    )
    assert names, "没有读到子命令的参数"
    for option in names:
        if "session" in option or "token" in option:
            assert option.endswith("-file"), f"{option} 直接接秘密，应该只接文件"
    with pytest.raises(SystemExit):
        parser.parse_args(["preflight", "--token", SESSION, "--order-file", "x", "--out", "y"])


def test_the_session_comes_from_a_private_file_and_is_checked_against_the_resolver_shape(tmp_path):
    module = _load()
    good = tmp_path / "session"
    good.write_text(SESSION + "\n")
    good.chmod(0o600)
    assert module.read_session(good) == SESSION

    for mode in (0o644, 0o640):
        good.chmod(mode)
        with pytest.raises(module.Refused):
            module.read_session(good)
        good.chmod(0o600)

    good.write_text("\n".join([SESSION, "second-line"]) + "\n")
    with pytest.raises(module.Refused):
        module.read_session(good)

    good.write_text("x" * 300 + "\n")
    with pytest.raises(module.Refused):
        module.read_session(good)

    target = tmp_path / "real-session"
    target.write_text(SESSION + "\n")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(module.Refused):
        module.read_session(link)


def _stack(tmp_path, token=SERVICE_TOKEN):
    config = tmp_path / "etc"
    (config / "gateway").mkdir(parents=True)
    runtime = config / "gateway/runtime.env"
    runtime.write_text(f"AIOPS_REDIS_HOST=127.0.0.1\nAIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN={token}\n")
    runtime.chmod(0o600)
    return config


def _inputs(tmp_path, *, order=ORDER_OWN, session=SESSION, token=SERVICE_TOKEN):
    config = _stack(tmp_path, token=token)
    session_file = tmp_path / "session"
    session_file.write_text(session + "\n")
    session_file.chmod(0o600)
    order_file = tmp_path / "order"
    order_file.write_text(order + "\n")
    order_file.chmod(0o600)
    state = tmp_path / "state"
    (state / "gateway").mkdir(parents=True)
    with sqlite3.connect(state / "gateway/gateway.db") as connection:
        connection.execute(
            "CREATE TABLE agent_runs (run_id TEXT, state TEXT, snapshot_json TEXT, "
            "expected_agent_version TEXT, observed_agent_version TEXT, exec_config_state TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (?, 'succeeded', '{}', 'abc', 'abc', 'matched')", (RUN_ID,)
        )
    return {
        "config": config,
        "session_file": session_file,
        "order_file": order_file,
        "out": tmp_path / "evidence",
        "tmp": tmp_path,
        "state": state,
    }


class _Gateway(BaseHTTPRequestHandler):
    """隔离 Gateway 替身：只计数、只看头，不发任何真实请求。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    status = 200
    body: dict = {}
    redirect_to: str | None = None
    redirect_hits = 0
    disconnect = False

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "session": self.headers.get("X-Third-Session"),
                "third_session": self.headers.get("third-session"),
                "entry": self.headers.get("X-Business-Entry"),
                "body": payload.decode(),
            }
        )
        if type(self).disconnect:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
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
def tree(tmp_path):
    """一份最小输入树：私有配置根、会话文件、订单文件、证据目录。"""
    return _inputs(tmp_path)


@pytest.fixture
def gateway():
    _Gateway.requests = []
    _Gateway.status = 200
    _Gateway.body = {
        "run_id": RUN_ID,
        "state": "succeeded",
        "agent_version": "abc",
        "agent_version_observed": "abc",
        "agent_version_gap": None,
    }
    _Gateway.redirect_to = None
    _Gateway.redirect_hits = 0
    _Gateway.disconnect = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _argv(tree, base_url, *, command="preflight", out=None):
    argv = [
        command,
        "--session-file",
        str(tree["session_file"]),
        "--order-file",
        str(tree["order_file"]),
        "--stack-config",
        str(tree["config"]),
        "--base-url",
        base_url,
        "--out",
        str(out or tree["out"]),
    ]
    if command == "run":
        argv.extend(["--state", str(tree["state"])])
    return argv


@pytest.fixture(autouse=True)
def container_logs(monkeypatch):
    # 外部 Docker 日志在单独的 live_replay 测试中覆盖；这里没有真实容器。
    module = _load()
    monkeypatch.setattr(module.live_replay, "docker_logs", lambda _: ("", 0))


def test_preflight_makes_no_request_at_all(gateway, tree):
    """默认只查前置：一次请求都不发（变异「preflight 里顺手探一下」会红）。"""
    module = _load()
    assert module.main(_argv(tree, gateway)) == 0
    # 两种方法都要为空：只数 POST 会让「preflight 顺手 GET 一下」这类变异漏过去。
    assert _Gateway.requests == [] and _Gateway.redirect_hits == 0
    payload = json.loads((tree["out"] / "preflight.json").read_text())
    assert payload["sent_requests"] == 0


def test_preflight_evidence_carries_no_credential(gateway, tree, capsys):
    """证据里既没有会话值也没有服务令牌 —— 只有摘要与计划。"""
    module = _load()
    assert module.main(_argv(tree, gateway)) == 0
    text = (tree["out"] / "preflight.json").read_text()
    assert SESSION not in text and SERVICE_TOKEN not in text
    assert ORDER_OWN not in text
    assert ORDER_OWN not in capsys.readouterr().out
    payload = json.loads(text)
    assert payload["preflight"]["headers"] == [
        "Authorization",
        "X-Third-Session",
        "X-Business-Entry: consumer",
    ]


def test_the_send_carries_the_session_as_the_internal_header_and_the_service_token_as_bearer(gateway, tree):
    """X-Third-Session 携带用户会话，Authorization 携带服务令牌。"""
    module = _load()
    assert module.main(_argv(tree, gateway, command="run")) == 0
    assert len(_Gateway.requests) == 1
    request = _Gateway.requests[0]
    assert request["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert request["session"] == SESSION
    assert request["entry"] == "consumer"
    assert request["body"] == "{}"
    assert request["path"] == f"/v1/orders/{ORDER_OWN}/charging-status/delegated"


def test_run_claims_before_sending_and_refuses_a_second_send(gateway, tree):
    module = _load()
    assert module.main(_argv(tree, gateway, command="run")) == 0
    assert (tree["out"] / ".send.claim").exists()
    assert module.main(_argv(tree, gateway, command="run")) == 3
    assert len(_Gateway.requests) == 1, "拒绝执行却仍发出了请求"


def test_a_redirect_is_refused_rather_than_followed_with_the_credentials(gateway, tree):
    module = _load()
    _Gateway.redirect_to = gateway + "/elsewhere"
    assert module.main(_argv(tree, gateway, command="run")) == 1
    assert _Gateway.redirect_hits == 0
    assert len(_Gateway.requests) == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "https://supplier.example/v1",
        "http://169.254.169.254",
        "http://127.0.0.1:8794/x",
        "file:///etc/passwd",
    ],
)
def test_a_non_loopback_target_is_refused_before_any_request(tree, base_url):
    module = _load()
    _Gateway.requests = []
    assert module.main(_argv(tree, base_url, command="run")) == 3
    assert _Gateway.requests == []


def test_a_missing_service_token_is_a_refusal_not_a_guess(tmp_path):
    """服务令牌只从栈的私有配置读；缺它没有任何回落（例如退回握手令牌）。"""
    module = _load()
    tree = _inputs(tmp_path)
    (tree["config"] / "gateway/runtime.env").write_text("AIOPS_REDIS_HOST=127.0.0.1\n")
    with pytest.raises(module.Refused):
        module.preflight(
            session_file=tree["session_file"],
            order_file=tree["order_file"],
            stack_config=tree["config"],
            base_url="http://127.0.0.1:8794",
        )


def test_a_secret_that_leaks_into_a_payload_is_redacted_or_the_write_is_refused(tmp_path):
    """即使上游回显会话，写盘前也要抹掉。"""
    module = _load()
    out = tmp_path / "evidence"
    path = module._write(out, "x.json", {"body": {"echo": SESSION}}, secrets=(SESSION,))
    text = path.read_text()
    assert SESSION not in text and "***" in text

    path = module._write(out, "y.json", {"body": "no secret here"}, secrets=(SERVICE_TOKEN,))
    assert SERVICE_TOKEN not in path.read_text()


def test_the_tool_never_picks_a_session_from_redis():
    """源码级钉子：不连 Redis、不扫描会话键 —— 会话是运营交过来的那一条。"""
    source = TOOL.read_text(encoding="utf-8")
    for forbidden in ("scan_iter", "import redis", "3rd_session"):
        assert forbidden not in source, f"工具开始自己挑会话了：{forbidden}"


# ---------------------------------------------------------------- 真实解析器链路
class _RedisStub:
    """只替换会话存储；真实解析器通过 RESP GET 读取，未知键回 nil。"""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.get_keys: list[str] = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.listener.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        buffer = b""
        with connection:
            while True:
                try:
                    chunk = connection.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while True:
                    parsed, buffer = self._command(buffer)
                    if parsed is None:
                        break
                    name, args = parsed
                    if name == "GET":
                        self.get_keys.append(args[0])
                        value = self.store.get(args[0]) if args else None
                        if value is None:
                            connection.sendall(b"$-1\r\n")
                        else:
                            connection.sendall(b"$%d\r\n%s\r\n" % (len(value), value))
                    else:
                        connection.sendall(b"+OK\r\n")

    @staticmethod
    def _command(buffer: bytes):
        if not buffer.startswith(b"*"):
            return None, buffer
        header_end = buffer.find(b"\r\n")
        if header_end < 0:
            return None, buffer
        count = int(buffer[1:header_end])
        index = header_end + 2
        parts: list[str] = []
        for _ in range(count):
            end = buffer.find(b"\r\n", index)
            if end < 0:
                return None, buffer
            size = int(buffer[index + 1 : end])
            start = end + 2
            if len(buffer) < start + size + 2:
                return None, buffer
            parts.append(buffer[start : start + size].decode())
            index = start + size + 2
        return (parts[0].upper(), parts[1:]), buffer[index:]


class _Orders:
    """订单授权替身：只接受同租户、同 C 端用户的 self 范围。"""

    OWNERSHIP = {ORDER_OWN: C_OWNER, ORDER_OTHER: C_OTHER}
    calls: list[tuple[str, str]] = []

    def can_access(self, context, order_no):
        type(self).calls.append((context.subject.c_user_id or "", order_no))
        return (
            context.data_scope.type == "self"
            and context.effective_tenant_id == TENANT
            and self.OWNERSHIP.get(order_no) == context.subject.c_user_id
        )


class _Dispatcher:
    """平台替身经真实领取、完成路由模拟节点回呼，不调用模型。"""

    calls: list[str] = []
    claim = None

    def dispatch(self, config, run_id: str) -> str:
        type(self).calls.append(run_id)
        if type(self).claim is not None:
            type(self).claim(run_id)
        return "ok"


def _write_agent_run_config(tmp_path: Path) -> Path:
    path = tmp_path / "agent-runs.json"
    path.write_text(
        json.dumps(
            {
                "environment": "env-a",
                "snail_base_url": "http://127.0.0.1:1",
                "snail_app_id": "aiops-alpha",
                "snail_app_token": "app-token-alpha",
                "snail_agent_id": 7,
                "snail_open_id": "open-id-alpha",
                "expected_node_id": "node-a1",
                "nodes": [
                    {
                        "node_id": "node-a1",
                        "environment": "env-a",
                        "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


@contextmanager
def _cpms_stub():
    """CPMS HTTP 替身，只返回本人订单的合成快照。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            prefix = "/charging-pile/chOrderInfo/chargingInfo/"
            order_no = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
            payload = (
                {"code": 0, "msg": "success", "data": {"orderNo": order_no, "status": "0", "plateNo": "X"}}
                if order_no == ORDER_OWN
                else {"code": 1, "msg": "not found", "data": None}
            )
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/charging-pile"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _live_gateway(tmp_path, redis_port: int, cpms_base: str):
    """不传 caller_resolver；正式工厂构造真实 RedisThirdSessionResolver。"""
    from test_m3_gateway_exec_config import _write_manifest

    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_store import GatewayStore

    config = tmp_path / "runtime.env"
    config.write_text(
        "\n".join(
            [
                "AIOPS_REDIS_HOST=127.0.0.1",
                f"AIOPS_REDIS_PORT={redis_port}",
                "AIOPS_REDIS_PASSWORD=stub-password",
                f"AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN={SERVICE_TOKEN}",
                "AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX=app:3rd_session:",
            ]
        )
        + "\n"
    )
    config.chmod(0o600)
    manifest = _write_manifest(tmp_path)
    identity = hashlib.sha256(manifest.read_bytes()).hexdigest()
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "state/gateway/gateway.db",
        server_config_file=config,
        # 会话链的启用门：它非空时 `_caller_resolver` 才构造真的 Redis 解析器。
        third_session_service_token=SERVICE_TOKEN,
        charging_status_base_url=cpms_base,
        execution_environment="env-a",
        agent_run_config_file=str(_write_agent_run_config(tmp_path)),
        execution_manifest_file=str(manifest),
    )
    # 工具文件输入夹具的库只供 HTTP 桩；真实 Gateway 必须自己创建真实 schema。
    settings.database_file.parent.mkdir(parents=True, exist_ok=True)
    settings.database_file.unlink(missing_ok=True)
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        order_authorizer=_Orders(),
    )
    # 解析器必须是真的那个 —— 否则这一组就退化成「测了一个替身」。
    assert type(app.state.gateway.caller_resolver).__name__ == "RedisThirdSessionResolver"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            # 平台在「派发期间」让节点来领一次：必须是同一个 app、走真实领取判据。
            client = TestClient(app)

            def claim(run_id: str) -> None:
                from aiops_diagnostics.agent_runs import AgentRunDispatchError

                response = client.post(
                    f"/v1/internal/charging-status/claims/{run_id}",
                    headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-AI-Ops-Exec-Config": identity},
                    json={},
                )
                if response.status_code != 200:
                    raise AgentRunDispatchError("claim failed")
                row = app.state.gateway.agent_run_store.get(run_id)
                assert row.state == "running" and row.snapshot is not None
                done = client.post(
                    f"/v1/internal/charging-status/completions/{run_id}",
                    headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-AI-Ops-Exec-Config": identity},
                    json={"status": "completed"},
                )
                assert done.status_code == 200

            _Dispatcher.claim = claim
            yield base
        finally:
            client.close()
            _Dispatcher.claim = None
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def _java_session(user_id: str, tenant_id: str = TENANT) -> bytes:
    """Java 序列化 String 的头、UTF-8 字节数与 JSON（本夹具仅用 ASCII）。"""
    body = json.dumps({"userId": user_id, "tenantId": tenant_id}).encode()
    return b"\xac\xed\x00\x05t" + len(body).to_bytes(2, "big") + body


@pytest.fixture
def redis_stub():
    stub = _RedisStub()
    yield stub
    stub.close()


@pytest.fixture
def clean_stubs(monkeypatch):
    _Orders.calls = []
    _Dispatcher.calls = []
    monkeypatch.setattr("aiops_diagnostics.gateway_api.SnailDispatcher", lambda: _Dispatcher())
    return _Dispatcher


def _runs(tmp_path: Path) -> list[dict]:
    database = tmp_path / "state/gateway/gateway.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs").fetchall()]
    finally:
        connection.close()


def test_real_session_of_the_order_owner_is_accepted_and_dispatched(tmp_path, redis_stub, clean_stubs):
    """正对照：真会话 → 本人范围 → 委派入口接受 → 一次派发。"""
    redis_stub.store[f"app:3rd_session:{SESSION}"] = _java_session(C_OWNER)
    tree = _inputs(tmp_path)
    module = _load()
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        assert module.main(_argv(tree, base, command="run", out=tree["out"])) == 0
    assert len(_Dispatcher.calls) == 1
    runs = _runs(tmp_path)
    assert len(runs) == 1 and runs[0]["state"] == "succeeded"
    row = runs[0]
    identity = hashlib.sha256((tmp_path / "exec-manifest.json").read_bytes()).hexdigest()
    assert row["exec_config_state"] == "matched"
    assert row["expected_agent_version"] == row["observed_agent_version"] == identity
    assert row["snapshot_json"] is not None and row["completed_at"] is not None
    assert redis_stub.get_keys == [f"app:3rd_session:{SESSION}"]


def test_an_unknown_session_is_rejected_without_dispatching(tmp_path, redis_stub, clean_stubs):
    tree = _inputs(tmp_path, session="expired-or-unknown")
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        assert _load().main(_argv(tree, base, command="run")) == 1
    assert json.loads((tree["out"] / "sent.json").read_text())["http_status"] == 401
    assert redis_stub.get_keys == ["app:3rd_session:expired-or-unknown"]
    assert _Dispatcher.calls == [] and _Orders.calls == [] and _runs(tmp_path) == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer not-the-service-token", "X-Third-Session": SESSION},
        {"Authorization": f"Bearer {SERVICE_TOKEN}"},
    ],
    ids=["wrong-service-token", "missing-session"],
)
def test_invalid_credentials_are_rejected_before_redis_get(tmp_path, redis_stub, clean_stubs, headers):
    redis_stub.store[f"app:3rd_session:{SESSION}"] = _java_session(C_OWNER)
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        response = httpx.post(
            f"{base}/v1/orders/{ORDER_OWN}/charging-status/delegated",
            headers={**headers, "X-Business-Entry": "consumer"},
            json={},
            trust_env=False,
        )
    assert response.status_code == 401
    assert redis_stub.get_keys == []
    assert _Dispatcher.calls == [] and _Orders.calls == [] and _runs(tmp_path) == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # 缺入口头：不默认成本人。
        ({"X-Business-Entry": None}, 403),
        # 管家端入口：入口判据要求恰好 consumer。
        ({"X-Business-Entry": "operator"}, 403),
    ],
)
def test_the_entry_header_must_say_consumer(tmp_path, redis_stub, clean_stubs, headers, expected):
    redis_stub.store[f"app:3rd_session:{SESSION}"] = _java_session(C_OWNER)
    sent = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Third-Session": SESSION,
        "X-Business-Entry": "consumer",
    }
    for key, value in headers.items():
        if value is None:
            sent.pop(key, None)
        else:
            sent[key] = value
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        response = httpx.post(
            f"{base}/v1/orders/{ORDER_OWN}/charging-status/delegated", headers=sent, json={}, trust_env=False
        )
    assert response.status_code == expected
    assert _Dispatcher.calls == [] and _runs(tmp_path) == []


def test_another_persons_order_is_refused_for_a_real_session(tmp_path, redis_stub, clean_stubs):
    """有效会话访问非本人订单：404，零派发、零运行记录。"""
    redis_stub.store[f"app:3rd_session:{SESSION}"] = _java_session(C_OWNER)
    tree = _inputs(tmp_path, order=ORDER_OTHER)
    module = _load()
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        assert module.main(_argv(tree, base, command="run", out=tree["out"])) == 1
    assert json.loads((tree["out"] / "sent.json").read_text())["http_status"] == 404
    assert _Dispatcher.calls == [], "被拒的请求走到了平台派发"
    assert _Orders.calls == [(C_OWNER, ORDER_OTHER)]
    assert _runs(tmp_path) == []


def test_a_session_that_belongs_to_another_tenant_cannot_see_the_order(tmp_path, redis_stub, clean_stubs):
    """会话是真的、但属于另一个租户：订单不可见（同一条 404，不区分原因）。"""
    redis_stub.store[f"app:3rd_session:{SESSION}"] = _java_session(C_OWNER, "TENANT-OTHER")
    tree = _inputs(tmp_path)
    module = _load()
    with _cpms_stub() as cpms, _live_gateway(tmp_path, redis_stub.port, cpms) as base:
        assert module.main(_argv(tree, base, command="run", out=tree["out"])) == 1
    assert json.loads((tree["out"] / "sent.json").read_text())["http_status"] == 404
    assert _Dispatcher.calls == [] and _runs(tmp_path) == []


def test_run_refuses_to_report_success_without_record_verification(gateway, tree):
    """不给 `--state` 就不声称核对过记录 —— 「没观测」不等于「核对通过」。"""
    module = _load()
    argv = _argv(tree, gateway, command="run")
    argv = argv[:-2]  # remove --state and its value
    with pytest.raises(SystemExit):
        module.main(argv)
    assert _Gateway.requests == []


def test_the_evidence_of_a_real_run_carries_neither_credential(gateway, tree):
    module = _load()
    assert module.main(_argv(tree, gateway, command="run")) == 0
    for path in tree["out"].iterdir():
        if path.is_file():
            text = path.read_text()
            assert SESSION not in text, f"{path.name} 里出现了会话值"
            assert SERVICE_TOKEN not in text, f"{path.name} 里出现了服务令牌"
            assert ORDER_OWN not in text, f"{path.name} 里出现了订单号"


@pytest.mark.parametrize("value", ["two words", "line\rbreak", "tab\tvalue", "中文", "bad\x7f"])
def test_session_rejects_unsafe_header_values(tree, value):
    tree["session_file"].write_text(value)
    with pytest.raises(_load().Refused):
        _load().read_session(tree["session_file"])


def test_service_token_uses_process_config_before_runtime_file(tree):
    path = tree["config"] / "gateway/gateway.env"
    path.write_text("AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=process-token\n")
    path.chmod(0o600)
    assert _load().read_service_token(tree["config"]) == "process-token"
    path.write_text("AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=\n")
    assert _load().read_service_token(tree["config"]) == SERVICE_TOKEN


def test_disconnection_keeps_claim_and_never_resends(gateway, tree):
    _Gateway.disconnect = True
    module = _load()
    assert module.main(_argv(tree, gateway, command="run")) == 1
    assert module.main(_argv(tree, gateway, command="run")) == 3
    assert len(_Gateway.requests) == 1
    assert json.loads((tree["out"] / "sent.json").read_text())["http_status"] is None


def test_concurrent_runs_send_at_most_once(gateway, tree):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    module = _load()

    def run():
        barrier.wait(timeout=5)
        return module.main(_argv(tree, gateway, command="run"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == [0, 3]
    assert len(_Gateway.requests) == 1


@pytest.mark.parametrize(
    "broken", ["missing-record", "failed", "no-snapshot", "identity", "logs", "substitution"]
)
def test_incomplete_or_mismatched_evidence_cannot_confirm(gateway, tree, monkeypatch, broken):
    module = _load()
    database = tree["state"] / "gateway/gateway.db"
    with sqlite3.connect(database) as connection:
        if broken == "missing-record":
            connection.execute("DELETE FROM agent_runs")
        elif broken == "failed":
            connection.execute("UPDATE agent_runs SET state='failed'")
        elif broken == "no-snapshot":
            connection.execute("UPDATE agent_runs SET snapshot_json=NULL")
        elif broken == "identity":
            connection.execute("UPDATE agent_runs SET observed_agent_version='different'")
    if broken == "logs":
        monkeypatch.setattr(module.live_replay, "docker_logs", lambda _: ("", 1))
    elif broken == "substitution":
        monkeypatch.setattr(
            module.live_replay,
            "docker_logs",
            lambda _: (json.dumps({"substituted": ["caller_resolver"]}), 0),
        )
    assert module.main(_argv(tree, gateway, command="run")) == 1
    assert not json.loads(next(tree["out"].glob("*.collect.json")).read_text())["confirmed"]


def test_proxy_environment_does_not_receive_credentials(gateway, tree, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert _load().main(_argv(tree, gateway, command="run")) == 0
    assert len(_Gateway.requests) == 1


@pytest.mark.parametrize("secret", ['token-with-"quote', "token-with-\\slash"])
def test_json_escaped_credentials_are_redacted(tmp_path, secret):
    path = _load()._write(tmp_path / "evidence", "escaped.json", {"echo": secret}, secrets=(secret,))
    assert json.loads(path.read_text())["echo"] == "***"
