#!/usr/bin/env python3
"""真实隧道验证：用 `dev-host exec` 建的隧道读一个**静态资源**。

不碰业务 API —— 只取 H5 站点的 `/index.html`，证明"隧道真的通了、目标是那台机器"。
业务接口一个请求都不发。

退出码：0 通过 / 1 判据不过 / 2 无法验证
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).parent
HOST = "ulink.h5.mall.qushiyun.com"
STATIC_PATH = "/index.html"


def _load_probe():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sp_tunnel", ROOT / "status_probe.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    probe = _load_probe()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    if probe._wait_port(port, timeout=0.2):
        print(f"端口 {port} 已被占用", file=sys.stderr)
        return 2

    tunnel = subprocess.Popen(
        probe.tunnel_argv(target="41", port=port),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    tunnel_pgid = os.getpgid(tunnel.pid)
    try:
        if not probe._wait_port(port, timeout=15):
            err = ""
            if tunnel.poll() is not None and tunnel.stderr is not None:
                err = (tunnel.stderr.read() or "").strip()[:300]
            print(f"隧道没起来（退出码 {tunnel.poll()}）：{err}", file=sys.stderr)
            return 2

        listening = probe._listening_pids(port)
        owned = bool(listening & probe._descendants(tunnel.pid))

        # 只读一个静态资源：TLS + SNI，走隧道到 41 本机 443。
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        request = f"GET {STATIC_PATH} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\nAccept: */*\r\n\r\n"
        body = b""
        status_line = b""
        with (
            socket.create_connection(("127.0.0.1", port), timeout=15) as raw,
            context.wrap_socket(raw, server_hostname=HOST) as tls,
        ):
            tls.sendall(request.encode())
            chunks = []
            while True:
                piece = tls.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
            body = b"".join(chunks)
        status_line = body.split(b"\r\n", 1)[0].decode(errors="replace")

        out = {
            "port": port,
            "dev_host_pid": tunnel.pid,
            "listening_pids": sorted(listening),
            "listener_in_this_process_tree": owned,
            "status_line": status_line,
            "bytes": len(body),
            "is_html": b"<!DOCTYPE html" in body[:200] or b"<html" in body[:400],
            "business_requests_sent": 0,
        }
        ok = owned and " 200 " in f" {status_line} " and out["is_html"]
        out["verdict"] = "pass" if ok else "fail"
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if ok else 1
    finally:
        probe._terminate_tree(tunnel, pgid=tunnel_pgid, port=port)
        time.sleep(0.5)
        leftover = probe._listening_pids(port)
        print(
            json.dumps({"cleanup": {"port": port, "still_listening": sorted(leftover)}}),
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
