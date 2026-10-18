"""会话库探测：解析必须与 Gateway 一致，认证判断必须逐字。

上一版的两个错误都在这里钉住：
- 自己 `split("=")` 会把引号当密码内容（Gateway 用 dotenv，会去掉引号）；
- 用子串匹配判断 AUTH 成功不能替代密码相等。
"""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from m2_live import redis_probe


class _Redis:
    """最小 Redis 替身：记录收到的 AUTH 参数，按预期口令判定。"""

    def __init__(self, password: str, user: str = "") -> None:
        self.password = password
        self.user = user
        self.auth_args: list[tuple[str, ...]] = []
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.port = listener.getsockname()[1]
        threading.Thread(target=self._serve, args=(listener,), daemon=True).start()

    def _serve(self, listener: socket.socket) -> None:
        conn, _ = listener.accept()
        authenticated = False
        with conn:
            buffer = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while True:
                    parsed, buffer = _parse_command(buffer)
                    if parsed is None:
                        break
                    name, args = parsed
                    if name == "AUTH":
                        self.auth_args.append(tuple(args))
                        expected = (self.user, self.password) if self.user else (self.password,)
                        authenticated = tuple(args) == expected
                        conn.sendall(b"+OK\r\n" if authenticated else b"-ERR invalid password\r\n")
                    elif name == "PING":
                        conn.sendall(
                            b"+PONG\r\n" if authenticated else b"-NOAUTH Authentication required.\r\n"
                        )
                    else:
                        conn.sendall(b"+OK\r\n")


def _parse_command(buffer: bytes):
    """按 RESP 数组解析一条命令；不完整时返回 (None, 原样 buffer)。"""
    if not buffer.startswith(b"*"):
        return None, buffer
    end_of_header = buffer.find(b"\r\n")
    if end_of_header < 0:
        return None, buffer
    count = int(buffer[1:end_of_header])
    index = end_of_header + 2
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


def _settings(port: int, password: str, user: str = "") -> dict[str, str]:
    return {
        "AIOPS_REDIS_HOST": "127.0.0.1",
        "AIOPS_REDIS_PORT": str(port),
        "AIOPS_REDIS_DATABASE": "0",
        "AIOPS_REDIS_USER": user,
        "AIOPS_REDIS_PASSWORD": password,
    }


def test_a_quoted_password_from_the_config_file_is_unquoted(tmp_path):
    """Gateway 用 dotenv：引号不是口令的一部分。探针必须给出同样的值。"""
    config = tmp_path / "runtime.env"
    config.write_text('AIOPS_REDIS_PASSWORD="fixture-password"\nAIOPS_REDIS_HOST=127.0.0.1\n')
    settings = redis_probe.read_settings({"AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config)})
    assert settings["AIOPS_REDIS_PASSWORD"] == "fixture-password"


def test_a_quoted_password_authenticates_against_the_real_value(tmp_path):
    server = _Redis("fixture-password")
    config = tmp_path / "runtime.env"
    config.write_text(
        f'AIOPS_REDIS_PASSWORD="fixture-password"\n'
        f"AIOPS_REDIS_HOST=127.0.0.1\nAIOPS_REDIS_PORT={server.port}\n"
    )
    settings = redis_probe.read_settings({"AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config)})
    ok, detail = redis_probe.probe(settings)
    assert ok, detail
    assert server.auth_args == [("fixture-password",)], "AUTH 送出的字节不对"


def test_a_username_and_password_pair_is_sent_when_configured():
    server = _Redis("p@ss", user="app")
    ok, detail = redis_probe.probe(_settings(server.port, "p@ss", "app"))
    assert ok, detail
    assert server.auth_args == [("app", "p@ss")]


def test_environment_overrides_the_config_file(tmp_path):
    config = tmp_path / "runtime.env"
    config.write_text("AIOPS_REDIS_HOST=from-file\nAIOPS_REDIS_PASSWORD=file\n")
    settings = redis_probe.read_settings(
        {"AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config), "AIOPS_REDIS_HOST": "from-env"}
    )
    assert settings["AIOPS_REDIS_HOST"] == "from-env"
    assert settings["AIOPS_REDIS_PASSWORD"] == "file"


def test_duplicate_keys_take_the_last_value_like_dotenv(tmp_path):
    config = tmp_path / "runtime.env"
    config.write_text("AIOPS_REDIS_PASSWORD=first\nAIOPS_REDIS_PASSWORD=second\n")
    settings = redis_probe.read_settings({"AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config)})
    assert settings["AIOPS_REDIS_PASSWORD"] == "second"


def test_an_unauthenticated_ping_fails_when_a_password_is_required():
    server = _Redis("s3cret")
    ok, detail = redis_probe.probe(_settings(server.port, ""))
    assert not ok
    assert detail in {"unexpected reply", "auth rejected"}


def test_a_wrong_password_reports_without_leaking_it():
    server = _Redis("s3cret")
    ok, detail = redis_probe.probe(_settings(server.port, "wrong-password"))
    assert not ok
    assert "wrong-password" not in detail and "s3cret" not in detail


def test_an_auth_reply_that_merely_contains_ok_is_not_success():
    """`+OKAY` 不是 `+OK`：子串匹配会把失败当成功。"""

    class _Liar(_Redis):
        def _serve(self, listener):  # noqa: D401 - 直接回一个近似答复
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"+OKAY\r\n")
                conn.recv(4096)
                conn.sendall(b"+PONG\r\n")

    server = _Liar("x")
    ok, detail = redis_probe.probe(_settings(server.port, "x"))
    assert not ok, detail


def test_connection_errors_report_only_the_type():
    ok, detail = redis_probe.probe(_settings(1, "x"))
    assert not ok
    assert detail in {"TimeoutError", "ConnectionRefusedError", "OSError"}
