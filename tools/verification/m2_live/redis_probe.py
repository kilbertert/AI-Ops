"""会话库（Redis）的就绪探测：**用 Gateway 自己的配置解析**认证，只报告结论。

为什么不能另写一套解析：Gateway 用 `python-dotenv` 读 `AIOPS_GATEWAY_SERVER_CONFIG_FILE`
（`Settings.from_config`，`interpolate=False`），所以 `AIOPS_REDIS_PASSWORD="p@ss"` 的
生效值是 `p@ss`（引号是引号，不是密码的一部分）。上一版探针自己 `split("=")`，把引号
算进密码，于是**配置正确时认证失败**——探针自己制造了它要报告的错误。

同理，认证答复要按 Redis 协议**逐字**判 `+OK`，不能用子串匹配代替密码相等。

凭据来源与真正的 Gateway 相同：环境变量优先，其次只读配置文件。**只打印结论与异常
类型**，绝不打印口令或会话值。
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
    """与 `Settings.from_config` 同一条解析：dotenv 语义 + 环境变量优先。"""
    env = dict(os.environ if environ is None else environ)
    config_file = env.get("AIOPS_GATEWAY_SERVER_CONFIG_FILE", "")
    values: dict[str, str | None] = {}
    if config_file:
        path = Path(config_file)
        if path.is_file():
            from dotenv import dotenv_values

            # 与 Gateway 一致：不插值。引号由 dotenv 去掉，值本身保留。
            values = dict(dotenv_values(path, interpolate=False))
        elif "AIOPS_CONFIG_FILE" in env:
            raise SystemExit(f"配置文件不存在: {path}")
    merged: dict[str, str] = {}
    for key, default in DEFAULTS.items():
        raw = env.get(key)
        if raw is None:
            raw = values.get(key)
        merged[key] = (raw if raw is not None else default).strip()
    return merged


def _command(*parts: str) -> bytes:
    """RESP 数组形式：内联命令在代理/中继上可能被拒。"""
    out = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        encoded = part.encode()
        out.append(f"${len(encoded)}\r\n".encode() + encoded + b"\r\n")
    return b"".join(out)


def _reply(sock: socket.socket) -> bytes:
    return sock.recv(256)


def _ok(reply: bytes) -> bool:
    """Redis 的简单字符串成功答复必须是 `+OK`，逐字比较。"""
    return reply.rstrip(b"\r\n") == b"+OK"


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
                if not _ok(_reply(sock)):
                    return False, "auth rejected"
            if database and database != "0":
                sock.sendall(_command("SELECT", database))
                if not _ok(_reply(sock)):
                    return False, "select rejected"
            sock.sendall(_command("PING"))
            reply = _reply(sock)
        healthy = reply.rstrip(b"\r\n") == b"+PONG"
        return healthy, ("pong" if healthy else "unexpected reply")
    except OSError as exc:  # 只报类型：消息里可能带地址或口令
        return False, type(exc).__name__


def main() -> int:
    settings = read_settings()
    ok, detail = probe(settings)
    print(f"  {'ok  ' if ok else 'FAIL'} session store ({detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
