#!/usr/bin/env python3
"""试运行栈里的回环中继：把容器内的 ``127.0.0.1:PORT`` 转给同栈另一个容器。

存在的原因只有一个：``canonical_gateway_url`` 只允许对回环地址使用明文 HTTP
（远端必须是 HTTPS）。平台服务端不提供 TLS，而 Gateway 的绑定表又必须过这条校验。
中继让「地址是回环」与「服务在别的容器」同时成立，**不改变那条校验**，也不对外
监听 —— 它只绑 127.0.0.1，容器外和栈外都不可达。

用法（在隔离 Gateway 容器里）：
  python loopback_relay.py --listen 8900 --target platform:8900
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import threading


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    """单向转发；读到 EOF 时只关闭**写方向**。

    整条 `shutdown(SHUT_RDWR)` 会连反向管道一起掐断：客户端发完请求后 `shutdown(SHUT_WR)`
    是 HTTP 里合法且常见的写法，反方向的响应还没回来就被这里关掉了。半关闭（SHUT_WR）
    让对端读到 EOF 并正常收尾，反向数据仍然能走完。
    """
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            sink.sendall(chunk)
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)
    except OSError:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_RDWR)


def serve(
    listen_port: int,
    target_host: str,
    target_port: int,
    connect_timeout: float = 10.0,
    listen_host: str = "127.0.0.1",
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listen_host, listen_port))
    listener.listen(64)
    print(f"relay {listen_host}:{listen_port} -> {target_host}:{target_port}", flush=True)
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            continue
        try:
            upstream = socket.create_connection((target_host, target_port), timeout=connect_timeout)
        except OSError:
            client.close()
            continue
        # `create_connection(timeout=)` 设的是**整个 socket** 的超时，不只是握手。
        # 不清掉它，平台一次慢响应（冷启动实测 18s）就会让中继在 10s 时把连接掐断，
        # 上游那头看到的是一次「平台不可用」——中继自己制造了它要转发的错误。
        upstream.settimeout(None)
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, required=True)
    # 默认只绑回环：绑定表的 URL 校验只接受回环明文。变成共享网络上的
    # `gateway-host-relay` 时，监听地址由部署件显式给出，并被隔离门禁当作
    # 只读依赖（不是可写状态）。
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--target", required=True, help="host:port")
    args = parser.parse_args()
    host, _, port = args.target.partition(":")
    serve(args.listen, host, int(port), listen_host=args.listen_host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
