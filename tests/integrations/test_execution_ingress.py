"""真实双段TLS：只有同步派发可达，平台管理与业务身份不能穿透。"""

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
module = runpy.run_path(str(ROOT / "tools/content/render_execution_ingress.py"))
render, DISPATCH = module["render"], module["DISPATCH"]


@pytest.mark.parametrize(
    "changes",
    [
        {"upstream": "https://public.example:443"},
        {"upstream": "http://127.0.0.1:8900"},
        {"upstream": "https://127.0.0.1:8900/path"},
        {"upstream": "https://user@127.0.0.1:8900"},
        {"upstream": "https://8.8.8.8:443"},
        {"server_name": "platform;return 200"},
        {"ca_file": "/tmp/a;"},
        {"ca_file": "/tmp/../key"},
        {"sources": ["127.0.0.0/8"]},
        {"sources": []},
        {"sources": ["127.0.0.1", "127.0.0.1"]},
    ],
)
def test_invalid_deployment_parameters(changes):
    args = dict(
        upstream="https://127.0.0.1:8900",
        server_name="platform",
        ca_file="/private/ca.crt",
        sources=["127.0.0.1"],
    )
    with pytest.raises(ValueError):
        render(**(args | changes))


def test_real_tls_dispatch_boundary(tmp_path):
    binary = shutil.which("nginx")
    if not binary:
        pytest.skip("nginx未安装，无法证明TLS代理行为")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
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
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    seen, processes = [], []

    class Platform(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):  # noqa: N802
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers), payload))
            valid = (
                self.headers.get("Snail-Ai-App-Id") == "fixture-app"
                and self.headers.get("Snail-Ai-Token") == "fixture-token"
            )
            self.send_response(200 if valid else 401)
            self.end_headers()
            self.wfile.write(b'{"data":{"content":"fixture"}}')

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Platform)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    upstream.socket = tls.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    def start(source="127.0.0.1", tls_name="localhost", use_tls=True):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        folder = tmp_path / str(port)
        folder.mkdir()
        config = folder / "nginx.conf"
        config.write_text(
            f"pid {folder}/pid; error_log {folder}/error.log warn; events {{}} "
            "http { access_log off; server {\n"
            f"listen 127.0.0.1:{port} {'ssl' if use_tls else ''};\n"
            f"ssl_certificate {cert}; ssl_certificate_key {key};\n"
            "set_real_ip_from 127.0.0.1; real_ip_header X-Forwarded-For;\n"
            "location / { return 418; }\n"
            + render(f"https://127.0.0.1:{upstream.server_port}", tls_name, str(cert), [source])
            + "\n} }\n"
        )
        process = subprocess.Popen(
            [binary, "-e", "stderr", "-c", str(config), "-g", "daemon off;"],
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return f"{'https' if use_tls else 'http'}://localhost:{port}"
            except OSError:
                assert process.poll() is None, process.stderr.read().decode()
                time.sleep(0.02)
        pytest.fail("nginx未启动")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))),
    )
    payload = json.dumps({"sid": "run_fixture", "content": "只读查询"}).encode()

    def call(base, path=DISPATCH, method="POST", token="fixture-token", data=payload, **headers):
        request = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Snail-Ai-App-Id": "fixture-app", "Snail-Ai-Token": token, **headers},
        )
        try:
            response = opener.open(request, timeout=4)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status

    try:
        base = start()
        assert call(base, **{"Cookie": "private", "Authorization": "Bearer private", "tenant-id": "x"}) == 200
        assert seen[-1][0] == DISPATCH and seen[-1][2] == payload
        assert not {"cookie", "authorization", "tenant-id"} & {h.lower() for h in seen[-1][1]}
        assert call(base, token="wrong") == 401
        before = len(seen)
        assert call(base, token="") == 401
        assert call(base, method="GET") == 405
        for path in (
            "/snail-ai/app/page",
            "/snail-ai/openapi/v1/user/register",
            DISPATCH + "/",
            DISPATCH + "?token=secret-marker",
            "/snail-ai/openapi/v1/agent/chat/%73ync",
        ):
            assert call(base, path) == 404
        assert call(base, data=b"x" * 65537) == 413
        for path in (
            "/snail-ai/../upms/user/info",
            "/snail-ai/%2e%2e/upms/user/info",
            "/%73nail-ai/%2e%2e/upms/user/info",
            "/upms/user/info",
        ):
            assert call(base, path) == 404
        request = urllib.request.Request(base + "/upms/user/info")
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(request, timeout=4)
        assert error.value.code == 418
        assert call(start(source="192.0.2.10"), **{"X-Forwarded-For": "192.0.2.10"}) == 403
        assert call(start(use_tls=False)) == 403
        assert call(start(tls_name="wrong.example")) == 502
        assert len(seen) == before
        for log in tmp_path.rglob("*.log"):
            assert "secret-marker" not in log.read_text()
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
