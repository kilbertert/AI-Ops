"""证书与运行中容器的一致性：只比文件不够，要比**运行中**的两侧。

这里的 server 是一个本地 TLS 终结器，用测试自己生成的证书链 —— 用来验证
「服务端证书与期望文件不一致时必须报 FAIL」，以及「信任库缺当前 CA 时必须报 FAIL」。
"""

from __future__ import annotations

import ssl
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from m2_live import cert_probe


def _tls_server(tmp_path: Path) -> tuple[int, Path]:
    """起一个用临时自签证书的 TLS 服务端，返回 (端口, 证书路径)。"""
    cert = tmp_path / "server.pem"
    key = tmp_path / "server.key"
    import subprocess

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(16)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return port, cert


def _bundle(tmp_path: Path, ca: Path) -> Path:
    bundle = tmp_path / "bundle.crt"
    system = Path("/etc/ssl/certs/ca-certificates.crt")
    bundle.write_bytes((system.read_bytes() if system.is_file() else b"") + ca.read_bytes())
    return bundle


def test_matching_certificate_and_bundle_pass(tmp_path, monkeypatch):
    port, cert = _tls_server(tmp_path)
    bundle = _bundle(tmp_path, cert)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    problems = cert_probe.check(f"https://localhost:{port}", str(cert), str(cert), str(bundle))
    assert problems == []


def test_a_stale_served_certificate_is_reported(tmp_path, monkeypatch):
    """服务端换过证书、文件也换了，但进程还握着旧的 —— 必须报出来。"""
    port, cert = _tls_server(tmp_path)
    other = tmp_path / "expected.pem"
    other.write_bytes(cert.read_bytes())
    # 伪造「另一个证书」：用同一份文件改名不够，必须真的不同。
    import subprocess

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(tmp_path / "x.key"),
            "-out",
            str(other),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    bundle = _bundle(tmp_path, cert)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    problems = cert_probe.check(f"https://localhost:{port}", str(other), str(cert), str(bundle))
    assert any("旧证书" in p for p in problems)


def test_a_bundle_without_the_current_ca_is_reported(tmp_path, monkeypatch):
    port, cert = _tls_server(tmp_path)
    empty = tmp_path / "empty.crt"
    empty.write_bytes(b"")
    monkeypatch.setenv("SSL_CERT_FILE", str(_bundle(tmp_path, cert)))
    problems = cert_probe.check(f"https://localhost:{port}", str(cert), str(cert), str(empty))
    assert any("信任库" in p for p in problems)


def test_an_unreachable_platform_reports_a_type_only(tmp_path, monkeypatch):
    import subprocess

    cert = tmp_path / "c.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(tmp_path / "c.key"),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    problems = cert_probe.check("https://127.0.0.1:1", str(cert), str(cert), str(cert))
    assert problems and "ConnectionRefusedError" in problems[0] or "TimeoutError" in problems[0]
