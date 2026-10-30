"""回环中继的回归测试：慢响应不能被中继自己掐断。

这条曾经真的发生过：冷启动时平台一次响应 18 秒，而 `create_connection(timeout=10)`
把超时留在了整个 socket 上，中继 10 秒就断开，上游看到的是「平台不可用」。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "verification"))

from m2_live import loopback_relay


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_relay_forwards_a_slow_response_untouched():
    """握手超时设成 0.5s、上游回 1.5s：回包必须仍然到得了调用方。

    修复前 `create_connection(timeout=)` 的 0.5s 会留在 socket 上，中继在 0.5s 时
    读超时、把连接关掉 —— 上游看到的是「平台不可用」。
    """
    target = socket.socket()
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target_port = target.getsockname()[1]

    def slow_echo() -> None:
        conn, _ = target.accept()
        with conn:
            conn.recv(1024)
            time.sleep(1.5)
            conn.sendall(b"late")

    threading.Thread(target=slow_echo, daemon=True).start()
    listen_port = _free_port()
    threading.Thread(
        target=loopback_relay.serve,
        args=(listen_port, "127.0.0.1", target_port, 0.5),
        daemon=True,
    ).start()
    time.sleep(0.3)

    with socket.create_connection(("127.0.0.1", listen_port), timeout=5) as client:
        client.settimeout(5)
        client.sendall(b"ping")
        assert client.recv(64) == b"late"
    target.close()


def test_a_half_closed_client_still_receives_the_response():
    """客户端 `shutdown(SHUT_WR)` 之后，反方向必须还能把响应送回来。

    HTTP/1.1 客户端常常发完请求就半关闭；旧实现读到 EOF 时 `shutdown(SHUT_RDWR)`，
    反向管道同时被掐断，调用方拿到的是空响应而不是迟到的回复。
    """
    target = socket.socket()
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target_port = target.getsockname()[1]

    def echo_after_eof() -> None:
        conn, _ = target.accept()
        with conn:
            data = b""
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            time.sleep(0.2)
            conn.sendall(b"reply:" + data)

    threading.Thread(target=echo_after_eof, daemon=True).start()
    listen_port = _free_port()
    threading.Thread(
        target=loopback_relay.serve, args=(listen_port, "127.0.0.1", target_port), daemon=True
    ).start()
    time.sleep(0.3)

    with socket.create_connection(("127.0.0.1", listen_port), timeout=5) as client:
        client.settimeout(5)
        client.sendall(b"ping")
        client.shutdown(socket.SHUT_WR)
        assert client.recv(64) == b"reply:ping"
    target.close()
