"""会话库（Redis）的就绪探测：**用实际的只读配置认证**，只报告结果。

为什么不能只发未认证 `PING`：生产会话库启用密码时，未认证连接会收到
`NOAUTH Authentication required.`，于是一条本来健康的通路被报成 FAIL ——
这正是「部署件的探针自己制造了它要报告的错误」。

凭据来源与真正的 Gateway 相同：`AIOPS_REDIS_*`（进程环境优先，其次
`AIOPS_GATEWAY_SERVER_CONFIG_FILE` 指向的只读配置文件）。**只打印结论与错误类型**，
绝不打印口令或会话值。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

DEFAULTS = {
    "AIOPS_REDIS_HOST": "127.0.0.1",
    "AIOPS_REDIS_PORT": "6379",
    "AIOPS_REDIS_DATABASE": "0",
    "AIOPS_REDIS_USER": "",
    "AIOPS_REDIS_PASSWORD": "",
}


def read_settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    config_file = env.get("AIOPS_GATEWAY_SERVER_CONFIG_FILE", "")
    if config_file and Path(config_file).is_file():
        for line in Path(config_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value)
    return {key: env.get(key, default) for key, default in DEFAULTS.items()}


def _command(*parts: str) -> bytes:
    """RESP 数组形式：内联命令在代理/中继上可能被拒。"""
    out = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        encoded = part.encode()
        out.append(f"${len(encoded)}\r\n".encode() + encoded + b"\r\n")
    return b"".join(out)


def probe(settings: dict[str, str], host: str | None = None, port: int | None = None) -> tuple[bool, str]:
    """返回 (是否健康, 只含结论/类型的说明)。异常文本只保留类型名。"""
    target_host = host or settings["AIOPS_REDIS_HOST"]
    target_port = port or int(settings["AIOPS_REDIS_PORT"])
    password = settings.get("AIOPS_REDIS_PASSWORD", "")
    username = settings.get("AIOPS_REDIS_USER", "")
    database = settings.get("AIOPS_REDIS_DATABASE", "0")
    try:
        with socket.create_connection((target_host, target_port), timeout=5) as sock:
            if password:
                parts = ("AUTH", username, password) if username else ("AUTH", password)
                sock.sendall(_command(*parts))
                reply = sock.recv(256)
                if not reply.startswith(b"+OK"):
                    return False, "auth failed"
            if database and database != "0":
                sock.sendall(_command("SELECT", database))
                reply = sock.recv(256)
                if not reply.startswith(b"+OK"):
                    return False, "select failed"
            sock.sendall(_command("PING"))
            reply = sock.recv(256)
        return (b"PONG" in reply.upper()), ("pong" if b"PONG" in reply.upper() else "unexpected reply")
    except OSError as exc:  # 只报类型：消息里可能带地址或口令
        return False, type(exc).__name__


def main() -> int:
    settings = read_settings()
    ok, detail = probe(settings)
    print(f"  {'ok  ' if ok else 'FAIL'} session store ({detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
