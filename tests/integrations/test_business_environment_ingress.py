"""真实nginx验证环境分流；上游只用合成身份，凭据不接生产。"""

import json
import runpy
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
render = runpy.run_path(str(ROOT / "tools/content/render_business_ingress.py"))["render"]


def catalogue(a="http://127.0.0.1:1", b="http://127.0.0.1:2"):
    return {
        "schema": "aiops-business-ingress/1",
        "environments": [
            {"id": "env-a", "gateway_url": a, "ingress_key": "a" * 64, "company_source_key": "c" * 64},
            {"id": "env-b", "gateway_url": b, "ingress_key": "b" * 64, "company_source_key": "d" * 64},
        ],
    }


def request(port, key=None, **extra):
    headers = {"Authorization": "Bearer same-user-jwt", **extra}
    if key is not None:
        headers["X-AIOps-Environment-Key"] = key
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/assistant/questions", b"{}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as r:
        return r.code, None


@pytest.fixture
def nginx(tmp_path):
    binary = shutil.which("nginx")
    if not binary:
        pytest.skip("真实nginx未安装，本环境不证明分流")
    running = []

    def start(config):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        folder = tmp_path / str(port)
        folder.mkdir()
        full = f"pid {folder}/pid; error_log {folder}/errors; events {{}} http {{\n{config(port)}\n}}"
        path = folder / "nginx.conf"
        path.write_text(full)
        path.chmod(0o600)
        proc = subprocess.Popen(
            [binary, "-e", "stderr", "-c", str(path), "-g", "daemon off;"], stderr=subprocess.PIPE
        )
        running.append(proc)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return port
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                time.sleep(0.02)
        pytest.fail("nginx未就绪")

    yield start
    for proc in running:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def upstreams():
    running, seen = [], []

    def start(name, tls=None):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen.append((name, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                reply = json.dumps({"environment": name}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        running.append(server)
        return server, ("https" if tls else "http") + f"://127.0.0.1:{server.server_port}"

    yield start, seen
    for server in running:
        server.shutdown()
        server.server_close()


def test_real_nginx_routes_only_by_exact_trusted_key(nginx, upstreams):
    start, seen = upstreams
    _, a = start("A")
    _, b = start("B")
    port = nginx(lambda p: render(catalogue(a, b), f"127.0.0.1:{p}"))
    assert request(port, "a" * 64, **{"X-AIOps-Environment": "env-b", "X-AIOps-Source-Key": "forged"}) == (
        200,
        {"environment": "A"},
    )
    assert request(port, "b" * 64) == (200, {"environment": "B"})
    for key in (None, "unknown", "A" * 64, "a" * 64 + ", " + "b" * 64):
        assert request(port, key)[0] == 403
    # 实际重复头不能取首个有效值；nginx把它们合并后不得匹配凭据。
    import http.client

    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    client.putrequest("POST", "/v1/assistant/questions")
    client.putheader("Content-Length", "2")
    client.putheader("X-AIOps-Environment-Key", "a" * 64)
    client.putheader("X-AIOps-Environment-Key", "b" * 64)
    client.endheaders(b"{}")
    assert client.getresponse().status == 403
    client.close()
    assert request(port, "\ta" + "a" * 63 + " \t") == (200, {"environment": "A"})
    assert [s[0] for s in seen] == ["A", "B", "A"]
    for name, headers, body in seen:
        assert headers["Authorization"] == "Bearer same-user-jwt" and body == b"{}"
        assert "X-AIOps-Environment-Key" not in headers
        assert headers["X-AIOps-Environment"] == "env-" + name.lower()
        assert headers["X-AIOps-Source-Key"] == ("c" if name == "A" else "d") * 64


def test_failed_environment_does_not_retry_another(nginx, upstreams):
    start, seen = upstreams
    failed, a = start("A")
    _, b = start("B")
    port = nginx(lambda p: render(catalogue(a, b), f"127.0.0.1:{p}"))
    failed.shutdown()
    failed.server_close()
    assert request(port, "a" * 64)[0] == 502
    assert request(port, "b" * 64) == (200, {"environment": "B"})
    assert [r[0] for r in seen] == ["B"]


@pytest.mark.parametrize(
    "change",
    ["duplicate_key", "duplicate_id", "http_remote", "url_injection", "path", "key_injection", "self"],
)
def test_unsafe_catalogue_is_rejected(change):
    c = catalogue()
    a, b = c["environments"]
    if change == "duplicate_key":
        b["ingress_key"] = a["ingress_key"]
    elif change == "duplicate_id":
        b["id"] = a["id"]
    elif change == "http_remote":
        b["gateway_url"] = "http://remote.example.invalid"
    elif change == "url_injection":
        b["gateway_url"] = 'https://example.invalid";return 200;#'
    elif change == "path":
        b["gateway_url"] = "https://example.invalid/other"
    elif change == "key_injection":
        b["company_source_key"] = 'x";return 200;#'
    else:
        b["gateway_url"] = "http://127.0.0.1:9876"
    with pytest.raises(ValueError):
        render(c, "127.0.0.1:9876", trusted_ca="/tmp/ca.crt")


def test_https_upstream_verifies_the_declared_ca(nginx, upstreams, tmp_path):
    def cert(name, dns="localhost"):
        key, crt = tmp_path / (name + ".key"), tmp_path / (name + ".crt")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:" + dns,
                "-keyout",
                str(key),
                "-out",
                str(crt),
            ],
            check=True,
            capture_output=True,
        )
        return str(crt), str(key)

    trusted, key = cert("trusted")
    wrong, _ = cert("wrong")
    start, seen = upstreams
    _, a = start("A", (trusted, key))
    c = catalogue(a.replace("127.0.0.1", "localhost"))
    good = nginx(lambda p: render(c, f"127.0.0.1:{p}", trusted_ca=trusted))
    bad = nginx(lambda p: render(c, f"127.0.0.1:{p}", trusted_ca=wrong))
    assert request(good, "a" * 64) == (200, {"environment": "A"})
    assert request(bad, "a" * 64)[0] == 502
    assert len(seen) == 1


@pytest.mark.parametrize(
    "pair",
    [
        ("https://example.invalid", "https://EXAMPLE.INVALID:443"),
        ("http://127.0.0.1:1", "http://127.0.0.1:00001"),
    ],
)
def test_same_gateway_cannot_be_two_environments(pair):
    with pytest.raises(ValueError):
        render(catalogue(*pair), "127.0.0.1:9876", trusted_ca="/tmp/ca.crt")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1:3",
        "https://127.1:3",
        "https://127.0.0.1:3",
        "https://2130706433:3",
        "https://0x7f000001:3",
        "https://example..invalid:3",
        "http://127.0.0.1:0",
    ],
)
def test_unsafe_ip_or_dns_alias_is_rejected(url):
    with pytest.raises(ValueError):
        render(catalogue(url), "127.0.0.1:9876", trusted_ca="/tmp/ca.crt")
