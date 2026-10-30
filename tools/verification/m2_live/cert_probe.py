"""核对「运行中的容器」与「本栈证书文件」是否一致。

重新生成证书后，运行中的 Gateway 仍握着旧信任库、平台仍加载着旧证书：那一刻
文件全都对，握手却全失败。只比文件不够，要比**运行中**的两侧。

用法（在 Gateway 容器里，证书以只读方式挂载）：
  python cert_probe.py --platform-url https://platform:8900 \
      --expected-cert /etc/m2live-tls/platform.crt --expected-ca /etc/m2live-tls/ca.crt

只打印结论与异常类型；证书内容是公开材料，但仍不打印指纹细节。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import socket
import ssl
import sys
import urllib.parse
from pathlib import Path


def pem_der(path: str) -> bytes:
    text = Path(path).read_bytes()
    body = text.split(b"-----BEGIN CERTIFICATE-----", 1)[1].split(b"-----END CERTIFICATE-----", 1)[0]
    return base64.b64decode(b"".join(body.split()))


def pem_der_bytes(chunk: bytes) -> bytes:
    body = chunk.split(b"-----END CERTIFICATE-----", 1)[0]
    return base64.b64decode(b"".join(body.split()))


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def served_fingerprint(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    context = ssl.create_default_context()
    with context.wrap_socket(socket.create_connection((host, port), timeout=5), server_hostname=host) as sock:
        return fingerprint(sock.getpeercert(binary_form=True))


def check(platform_url: str, expected_cert: str, expected_ca: str, bundle: str) -> list[str]:
    problems: list[str] = []
    try:
        if served_fingerprint(platform_url) != fingerprint(pem_der(expected_cert)):
            problems.append("platform 仍在使用旧证书（重启 platform 与 gateway 后再试）")
    except OSError as exc:
        problems.append(f"证书核对失败: {type(exc).__name__}")
    # 比较**DER**：PEM 文本在 bundle 里可能因为换行/注释而不同，但那不代表证书不同。
    try:
        ca_der = pem_der(expected_ca)
        bundle_der = {
            pem_der_bytes(chunk)
            for chunk in Path(bundle).read_bytes().split(b"-----BEGIN CERTIFICATE-----")[1:]
        }
        if ca_der not in bundle_der:
            problems.append("gateway 的信任库不含当前 CA（重启 gateway 后再试）")
    except (OSError, IndexError):
        problems.append("读不到当前的信任库或 CA")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", default="https://platform:8900")
    parser.add_argument("--expected-cert", default="/etc/m2live-tls/platform.crt")
    parser.add_argument("--expected-ca", default="/etc/m2live-tls/ca.crt")
    parser.add_argument("--bundle", default="/data/ca-bundle.crt")
    args = parser.parse_args()

    problems = check(args.platform_url, args.expected_cert, args.expected_ca, args.bundle)
    if problems:
        for problem in problems:
            print(f"  FAIL {problem}")
        return 1
    print("  ok   platform 服务的是当前证书；gateway 信任库含当前 CA")
    return 0


if __name__ == "__main__":
    sys.exit(main())
