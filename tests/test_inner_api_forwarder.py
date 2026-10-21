"""`aiops-inner-api-forward`：每次连接现查地址，查不到就断（#889）。

六条判据，每条对着一条"转发器看起来在跑，其实转错了"：

1. **只绑回环**：非回环的 `--listen` 直接拒绝 —— 这个转发器把一个没 publish 的管理端口
   搬到宿主上，绑 `0.0.0.0` 就是把 5001 暴露出去。
2. **现查**：容器换 IP 后（最接近"容器重建"的模拟），**下一次连接**转到新地址。
3. **fail-closed**：查不到容器 ⇒ 连接被**关闭**（不是挂起、不是转到别处）。
4. **容器端口没起** ⇒ 连接被关闭（`ConnectionRefused`），调用方拿到明确的失败。
5. **双向字节一致**：请求原样到达上游，响应原样回来（含较大的 body）。
6. **单元文件与脚本的边界一致**：systemd 单元的 `ExecStart` 只绑回环、容器名可配置。

这条脚本**不进包**（`ops/` 不是 `src/`），所以测试直接按路径加载它。
"""

from __future__ import annotations

import importlib.util
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
FORWARDER = REPO / "ops/dify-inner-api/aiops-inner-api-forward.py"
UNIT = REPO / "ops/dify-inner-api/aiops-inner-api-forward.service"
TUNNEL = REPO / "ops/dify-inner-api/aiops-inner-api-tunnel.service"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("forwarder_under_test", FORWARDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module() -> Any:
    return _load()


class _Echo:
    """一个会原样回写的上游，记录它收到了什么。"""

    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received: list[bytes] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                # 逐个 chunk 回写直到对端关闭：只读一次的实现在大 body 上会被转发器
                # 提前关掉连接，而那是**替身**的问题，不是转发器的。
                while True:
                    try:
                        chunk = client.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.received.append(chunk)
                    client.sendall(chunk)

    def close(self) -> None:
        self.server.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_in_thread(module: Any, port: int, container: str, container_port: int) -> threading.Thread:
    thread = threading.Thread(
        target=module.serve, args=("127.0.0.1", port, container, container_port), daemon=True
    )
    thread.start()
    # 等它绑上端口（监听日志是 stdout，不在这里断言它）。
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return thread
        except OSError:
            time.sleep(0.02)
    raise AssertionError("forwarder never started listening")


def test_a_non_loopback_listen_address_is_refused(module) -> None:
    """绑 `0.0.0.0` 就是把一个没 publish 的管理端口暴露到宿主网络。"""
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        module._endpoint("0.0.0.0:15001")
    with pytest.raises(argparse.ArgumentTypeError):
        module._endpoint(":15001")
    assert module._endpoint("127.0.0.1:15001") == ("127.0.0.1", 15001)


def test_it_resolves_the_container_address_on_every_connection(module) -> None:
    """**现查**：上游换端口（最接近"容器重建换 IP"的可复现形态）后，下一次连接转到新地址。

    变异对照：把 `container_address` 的结果缓存在模块级变量里，本用例变红 —— 而
    缓存在生产上的表现是"容器重建后转发器一直往旧 IP 发，直到有人重启它"。
    """
    first, second = _Echo(), _Echo()
    upstreams = {"port": first.port}

    def fake_address(container: str, container_port: int) -> str:
        del container_port
        assert container == "deploy-api-1"
        return f"127.0.0.1:{upstreams['port']}"

    module.container_address = fake_address
    listen = _free_port()
    _serve_in_thread(module, listen, "deploy-api-1", 5001)

    with socket.create_connection(("127.0.0.1", listen), timeout=5) as client:
        client.sendall(b"one")
        assert client.recv(64) == b"one"

    upstreams["port"] = second.port  # 容器换了地址（重建）
    with socket.create_connection(("127.0.0.1", listen), timeout=5) as client:
        client.sendall(b"two")
        assert client.recv(64) == b"two", "转发器用了缓存的旧地址"

    assert b"".join(first.received) == b"one" and b"".join(second.received) == b"two"
    first.close()
    second.close()


def test_an_unknown_container_closes_the_connection_instead_of_hanging(module) -> None:
    """查不到容器 ⇒ **关闭**，且**不尝试连任何别的地方**。

    挂起会让调用方把"容器没了"读成"上游很慢"；转到别处则更糟 —— 它把一次管理调用
    发给了另一个进程。所以这里同时钉住两件事：连接被关闭，且 `create_connection`
    **一次都没被调用**。

    直接驱动 `handle()`（配一对 socket），因此这条用例的"客户端"不经过被替换的
    `create_connection` —— 否则那条断言会打到测试自己的连接上。
    """
    module.container_address = lambda container, container_port: None
    attempts: list[Any] = []

    def recording_connect(address: Any, **kwargs: Any) -> Any:
        attempts.append(address)
        raise AssertionError(f"fail-closed 时不该连接任何地址，实际连了 {address}")

    original = module.socket.create_connection
    module.socket.create_connection = recording_connect
    client_end, server_end = module.socket.socketpair()
    try:
        module.handle(server_end, "missing-container", 5001)
        client_end.settimeout(5)
        assert client_end.recv(64) == b"", "查不到容器时必须关闭连接"
    finally:
        module.socket.create_connection = original
        client_end.close()
        server_end.close()
    assert attempts == []


def test_a_dead_container_port_fails_closed(module) -> None:
    """容器地址查得到、端口没起 ⇒ 同样是关闭连接（ConnectionRefused 被吸收到"拒绝"）。"""
    module.container_address = lambda container, container_port: f"127.0.0.1:{_free_port()}"
    listen = _free_port()
    _serve_in_thread(module, listen, "deploy-api-1", 5001)

    with socket.create_connection(("127.0.0.1", listen), timeout=5) as client:
        client.settimeout(5)
        assert client.recv(64) == b""


def test_bytes_are_relayed_unchanged_in_both_directions(module) -> None:
    """请求原样到达、响应原样回来 —— 包括一个比单次 recv 更大的 body。"""
    echo = _Echo()
    module.container_address = lambda container, container_port: f"127.0.0.1:{echo.port}"
    listen = _free_port()
    _serve_in_thread(module, listen, "deploy-api-1", 5001)

    payload = b"x" * 200_000
    with socket.create_connection(("127.0.0.1", listen), timeout=10) as client:
        sender = threading.Thread(target=client.sendall, args=(payload,), daemon=True)
        sender.start()
        received = b""
        while len(received) < len(payload):
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
        sender.join(timeout=10)

    assert received == payload
    assert b"".join(echo.received) == payload
    echo.close()


def test_the_unit_files_keep_the_boundary() -> None:
    """单元文件与 README 里的边界必须一致：只绑回环、容器名可配置、隧道用受限账号。"""
    unit = UNIT.read_text(encoding="utf-8")
    assert "--listen 127.0.0.1:15001" in unit
    assert "0.0.0.0" not in unit
    assert "--container deploy-api-1" in unit

    tunnel = TUNNEL.read_text(encoding="utf-8")
    assert "-L 127.0.0.1:15001:127.0.0.1:15001" in tunnel
    assert "ExitOnForwardFailure=yes" in tunnel
    assert "-o BatchMode=yes" in tunnel, "交互式隧道会让单元挂在那里等密码"
    # 这一层不是授权：真正的门是 X-Inner-Api-Key，README 必须这么说。
    readme = (FORWARDER.parent / "README.md").read_text(encoding="utf-8")
    assert "这一层不是授权" in readme
    assert "5001" in readme and "永不 publish" in readme
