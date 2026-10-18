"""会话库探测：必须用**实际配置**认证；只看未认证 PING 会把健康通路报成失败。

`auth-required` fixture 是一个只接受 `AUTH` 之后 `PING` 的本地 socket，模拟生产
Redis 启用了密码的形态。它不需要真的 Redis，也不会碰任何真实端口。
"""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from m2_live import redis_probe


def _server(password: str) -> tuple[int, list[bytes]]:
    """最小 Redis 替身：未认证时对任何命令回 NOAUTH；认证后回 PONG。"""
    seen: list[bytes] = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        authenticated = False
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                seen.append(chunk)
                upper = chunk.upper()
                if b"AUTH" in upper:
                    authenticated = password.encode() in chunk
                    conn.sendall(b"+OK\r\n" if authenticated else b"-ERR invalid password\r\n")
                elif b"PING" in upper:
                    conn.sendall(b"+PONG\r\n" if authenticated else b"-NOAUTH Authentication required.\r\n")
                else:
                    conn.sendall(b"+OK\r\n")

    threading.Thread(target=serve, daemon=True).start()
    return port, seen


def _settings(port: int, password: str) -> dict[str, str]:
    return {
        "AIOPS_REDIS_HOST": "127.0.0.1",
        "AIOPS_REDIS_PORT": str(port),
        "AIOPS_REDIS_DATABASE": "0",
        "AIOPS_REDIS_USER": "",
        "AIOPS_REDIS_PASSWORD": password,
    }


def test_authenticated_ping_succeeds_when_a_password_is_required():
    port, seen = _server("s3cret")
    ok, detail = redis_probe.probe(_settings(port, "s3cret"))
    assert ok, detail
    assert any(b"AUTH" in chunk.upper() for chunk in seen), "探测没有先做认证"


def test_an_unauthenticated_ping_would_have_failed():
    """对照：同一台服务器，不给凭据时必须失败 —— 证明上面那条不是碰巧通过。"""
    port, _ = _server("s3cret")
    ok, detail = redis_probe.probe(_settings(port, ""))
    assert not ok
    assert detail in {"unexpected reply", "auth failed"}


def test_a_wrong_password_reports_failure_without_leaking_it():
    port, _ = _server("s3cret")
    ok, detail = redis_probe.probe(_settings(port, "wrong-password"))
    assert not ok
    assert "wrong-password" not in detail
    assert "s3cret" not in detail


def test_settings_prefer_the_environment_then_the_config_file(tmp_path, monkeypatch):
    config = tmp_path / "runtime.env"
    config.write_text("AIOPS_REDIS_HOST=from-file\nAIOPS_REDIS_PASSWORD=file-secret\n")
    env = {"AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config), "AIOPS_REDIS_HOST": "from-env"}
    settings = redis_probe.read_settings(env)
    assert settings["AIOPS_REDIS_HOST"] == "from-env", "环境变量应优先"
    assert settings["AIOPS_REDIS_PASSWORD"] == "file-secret"
    assert settings["AIOPS_REDIS_PORT"] == "6379"


def test_connection_errors_report_only_the_type():
    ok, detail = redis_probe.probe(_settings(1, "x"))
    assert not ok
    assert detail in {"TimeoutError", "ConnectionRefusedError", "OSError"}
