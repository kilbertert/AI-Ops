"""只绑回环的 CONNECT 代理：把 `dify.ranlei.work:443` 转到本机隧道端口（默认 18443）。

## 什么时候需要它

开发机到 `dify.ranlei.work:443` 的 TLS 握手会被重置（2026-11-04 实测，5 次全失败），
而 36 本机可达。要**用真实登录会话打开控制台页面**做验收时，走：

```bash
ssh -N -L 18443:127.0.0.1:443 yidong-36 &        # 隧道（只绑本机回环）
uv run python tools/verification/dify-console-tunnel/proxy.py --port 18080 &
# 浏览器/Playwright 用 proxy=http://127.0.0.1:18080，URL 仍是 https://dify.ranlei.work
```

**为什么不是直接访问 `https://dify.ranlei.work:18443`**：那样浏览器发的 `Origin` 会带上
`:18443`，同源桥的校验会拒绝（「请从Dify控制台操作」）。走 CONNECT 代理时浏览器看到的
仍是 `https://dify.ranlei.work`，`Origin` 正确，桥的校验照常生效 —— 这条差别是本工具存在的
理由，不是实现细节。

## 边界

- 只处理 `CONNECT`，只转发到回环上的隧道端口；不解析请求内容，不做任何鉴权。
- **它不修复网络**：隧道关掉即失效。用它产出的证据必须写明"经取证通道"。
- 只用 `_Host-` cookie 时，Playwright 必须用 `add_cookies([... {"url": BASE}])`：
  `__Host-` 前缀不允许带 `domain` 字段，带上就整条被丢弃（2026-11-04 实测：
  页面退回登录页，看起来像会话过期）。
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import threading

MAX_HEAD = 8192


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            target.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _handle(client: socket.socket, upstream: tuple[str, int]) -> None:
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
            if len(head) > MAX_HEAD:
                client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
        if not head.split(b"\r\n", 1)[0].startswith(b"CONNECT "):
            # 只支持 CONNECT：普通请求会把目标主机写进 Host 头，那等于把代理变成透明转发。
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        remote = socket.create_connection(upstream, timeout=10)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=_pipe, args=(client, remote), daemon=True).start()
        _pipe(remote, client)
    except OSError:
        pass
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--upstream-port", type=int, default=18443)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        # 绑到非回环就是把一条无鉴权的内网通道开给整个网段。
        raise SystemExit("proxy must bind loopback")

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(50)
    print(f"proxy {args.host}:{args.port} -> 127.0.0.1:{args.upstream_port}", flush=True)
    try:
        while True:
            client, _ = server.accept()
            threading.Thread(
                target=_handle, args=(client, ("127.0.0.1", args.upstream_port)), daemon=True
            ).start()
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
