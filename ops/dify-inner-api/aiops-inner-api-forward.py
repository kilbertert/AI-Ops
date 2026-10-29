#!/usr/bin/env python3
"""把宿主回环上的一个端口转发到 Dify ``api`` 容器的 ``5001``（#889）。

    aiops-inner-api-forward --listen 127.0.0.1:15001 --container deploy-api-1 [--container-port 5001]

## 为什么不是 nginx

宿主 nginx 解析不到 compose 的容器名（那是容器内的 ``127.0.0.11``），而**静态
``proxy_pass`` 的主机名只在加载配置时解析一次** —— 容器重建换了 IP 之后，写
``/etc/hosts`` 也救不了正在跑的 nginx，它会一直转发到旧 IP。所以这里**每次 accept 都现查**
地址：没有缓存，也没有需要 reload 的配置。

## 为什么不是 ``docker compose ports: 5001:5001``

那会把一个管理接口 publish 到宿主网络，正是要避免的事。5001 至今**没有** publish 到宿主。

## Fail-closed

查不到容器 IP（容器停了、改名了、docker 不可用）时**直接关闭这条连接**，不转发到别处、
也不挂起。调用方（``aiops-admin ensure-workspace``）因此拿到一个明确的连接失败，而不是
一个超时到天荒地老的等待。

## 这一层不是授权

隧道建立后，41 本机的**任何进程**都能连 ``127.0.0.1:15001`` —— ``permitopen`` 只限制 SSH
的远端目标，不限制本地。授权在 ``X-Inner-Api-Key`` 上，Dify 那边仍然要过它。

标准库实现（socket + select）；36 上没有 socat，也不需要引入一个。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import selectors
import socket
import subprocess
import sys
import time

#: 查不到地址时的重试次数与间隔。一次 ``docker inspect`` 偶发失败不该把连接直接掐掉，
#: 但也不该长时间挂着（那会让调用方的超时看起来像"上游很慢"）。
INSPECT_ATTEMPTS = 3
INSPECT_RETRY_SECONDS = 0.2
#: 建立上游连接的超时。容器活着但端口没起时，这一步给出明确的 ConnectionRefused。
CONNECT_TIMEOUT = 5.0
#: 单条连接的空闲上限。`ensure-workspace` 的一次调用是秒级的；把「永远不动的连接」
#: 收掉，避免一个挂死的客户端在转发器里留一个永久 fd。
IDLE_TIMEOUT = 120.0

BUFFER = 65536


def container_address(container: str, container_port: int) -> str | None:
    """``docker inspect`` 取容器的当前 IP；查不到返回 ``None``（**不猜、不缓存**）。"""
    template = "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}"
    for attempt in range(INSPECT_ATTEMPTS):
        try:
            completed = subprocess.run(
                ["docker", "inspect", "-f", template, container],
                capture_output=True,
                text=True,
                timeout=CONNECT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            # 一个容器可能同时在多个网络上；取第一个有地址的（与 `docker inspect` 的
            # 输出顺序一致，且同一个 compose 栈里各网络都通到 5001）。
            for item in completed.stdout.split():
                if "=" in item:
                    _, _, address = item.partition("=")
                    if address:
                        return f"{address}:{container_port}"
        if attempt + 1 < INSPECT_ATTEMPTS:
            time.sleep(INSPECT_RETRY_SECONDS)
    return None


def pump(client: socket.socket, upstream: socket.socket) -> None:
    """双向搬运，直到任一侧关闭或空闲超时。"""
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = selector.select(timeout=IDLE_TIMEOUT)
            if not events:  # 空闲超时：两侧都收掉
                return
            for key, _ in events:
                source: socket.socket = key.fileobj  # type: ignore[assignment]
                target: socket.socket = key.data
                chunk = source.recv(BUFFER)
                if not chunk:
                    return
                target.sendall(chunk)
    finally:
        selector.close()


def handle(client: socket.socket, container: str, container_port: int) -> None:
    target = container_address(container, container_port)
    if target is None:
        # Fail closed：查不到就断开。转到别处或挂起都会让调用方把"容器没了"读成别的东西。
        print(json.dumps({"refused": "container address unknown", "container": container}), file=sys.stderr)
        client.close()
        return
    host, _, port = target.rpartition(":")
    try:
        upstream = socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        print(
            json.dumps({"refused": type(exc).__name__, "target": target}),
            file=sys.stderr,
        )
        client.close()
        return
    try:
        client.settimeout(None)
        upstream.settimeout(None)
        pump(client, upstream)
    finally:
        for item in (client, upstream):
            with contextlib.suppress(OSError):
                item.close()


def serve(listen_host: str, listen_port: int, container: str, container_port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen_host, listen_port))
        server.listen(16)
        bound_host, bound_port = server.getsockname()[:2]
        print(
            json.dumps(
                {
                    "listening": f"{bound_host}:{bound_port}",
                    "container": container,
                    "container_port": container_port,
                    "note": "address resolved per connection; never published",
                }
            ),
            flush=True,
        )
        while True:
            client, _ = server.accept()
            try:
                handle(client, container, container_port)
            except Exception as exc:  # noqa: BLE001 - 一条坏连接不该带走监听
                print(json.dumps({"connection_failed": type(exc).__name__}), file=sys.stderr)
                with contextlib.suppress(OSError):
                    client.close()


def _endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        # 这条转发器**只该绑回环**：它是一个管理接口的入口，绑 0.0.0.0 就是把 5001 暴露出去。
        raise argparse.ArgumentTypeError("listen address must be loopback (127.0.0.1)")
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=_endpoint, default=("127.0.0.1", 15001))
    parser.add_argument("--container", required=True)
    parser.add_argument("--container-port", type=int, default=5001)
    args = parser.parse_args(argv)
    serve(args.listen[0], args.listen[1], args.container, args.container_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
