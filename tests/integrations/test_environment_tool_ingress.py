"""真实HTTPS扩展入口：限定实际连接来源、声明工具和固定本机上游。"""

import http.client
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
render = runpy.run_path(str(ROOT / "tools/content/render_node_callback.py"))["render_ingress"]


@pytest.mark.parametrize(
    ("gateway", "sources"),
    [
        ("https://remote.example:443", ["127.0.0.1"]),
        ("http://127.0.0.1:0", ["127.0.0.1"]),
        ("http://127.0.0.1:1;return 200", ["127.0.0.1"]),
        ("http://127.0.0.1:1", ["127.0.0.0/8"]),
        ("http://127.0.0.1:1", []),
        ("http://127.0.0.1:1", ["127.0.0.1", "127.0.0.1"]),
    ],
)
def test_reject_untrusted_deployment_parameters(gateway, sources):
    with pytest.raises(ValueError):
        render(gateway, sources)


def test_tls_extension_preserves_company_routes_and_restricts_tools(tmp_path):
    binary = shutil.which("nginx")
    if not binary:
        pytest.skip("nginx未安装，不能证明真实入口")
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

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            seen.append((self.path, dict(self.headers)))
            ok = self.headers.get("Authorization") == "Bearer fixture-run"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(json.dumps({"authorized": ok}).encode())

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def start(source, tls=True):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        folder = tmp_path / str(port)
        folder.mkdir()
        content = render(f"http://127.0.0.1:{server.server_port}", [source])
        config = folder / "nginx.conf"
        config.write_text(
            f"pid {folder}/pid; error_log {folder}/error.log warn; events {{}} "
            "http { access_log off; server {\n"
            f"listen 127.0.0.1:{port} {'ssl' if tls else ''};\n"
            f"ssl_certificate {cert}; ssl_certificate_key {key};\n"
            "set_real_ip_from 127.0.0.1; real_ip_header X-Forwarded-For;\n"
            "location / { return 418; }\n" + content + "\n} }\n"
        )
        proc = subprocess.Popen(
            [binary, "-e", "stderr", "-c", str(config), "-g", "daemon off;"], stderr=subprocess.PIPE
        )
        processes.append(proc)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return f"{'https' if tls else 'http'}://localhost:{port}"
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                time.sleep(0.02)
        pytest.fail("nginx未启动")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))),
    )

    def call(base, path, method="POST", token="fixture-run", **headers):
        req = urllib.request.Request(
            base + path,
            data=b"{}" if method == "POST" else None,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **headers},
        )
        try:
            response = opener.open(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status

    try:
        base = start("127.0.0.1")
        path = "/v1/internal/consumer-data/agent"
        assert call(base, path, token="") == 401
        assert call(base, path, **{"X-Third-Session": "forged", "X-AIOps-Source-Key": "forged"}) == 200
        assert seen[-1][1]["Content-Type"] == "application/json"
        assert not {"x-third-session", "x-aiops-source-key"} & {h.lower() for h in seen[-1][1]}
        assert call(base, "/v1/internal/execution-node/capabilities") == 200
        assert call(base, "/v1/internal/charging-status/dify-read") == 200
        prefix = "/v1/internal/operator-runs/run_probe"
        from aiops_diagnostics.operator import operator_tools

        for tool in operator_tools.tools():
            route = tool.path_template
            for name in tool.inputs:
                route = route.replace("{" + name + "}", "example")
            assert call(base, prefix + "/cpms" + route, "GET") == 200
            assert (
                call(
                    base,
                    prefix + f"/tools/{tool.name}/authorization",
                    **{"X-AI-Ops-Exec-Config": "digest", "X-AI-Ops-Tool-Contract": "contract"},
                )
                == 200
            )
            assert seen[-1][1]["X-AI-Ops-Exec-Config"] == "digest"
            assert seen[-1][1]["X-AI-Ops-Tool-Contract"] == "contract"
        before = len(seen)
        for bad in (prefix + "/cpms/unknown", prefix + "/tools/not_declared/evidence", "/v1/internal/admin"):
            assert call(base, bad) == 404
        assert call(base, path, "GET") == 405
        assert call(base, "/v1/internal/charging-status/dify-read", "GET") == 405
        assert call(base, prefix + "/cpms/chSite/page", "HEAD") == 405
        assert call(base, "/v1/internal/consumer-knowledge/catalogue", "HEAD") == 405
        assert call(base, "/v1/assistant/questions") == 418
        assert call(base, "/upms/user/info", "GET") == 418
        assert len(seen) == before
        conn = http.client.HTTPSConnection(
            "localhost", int(base.rsplit(":", 1)[1]), context=ssl.create_default_context(cafile=str(cert))
        )
        try:
            conn.putrequest("POST", path)
            conn.putheader("Authorization", "Bearer duplicate-secret-marker")
            conn.putheader("Authorization", "Bearer another-secret-marker")
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            assert conn.getresponse().status == 400
        finally:
            conn.close()
        assert len(seen) == before
        for log in tmp_path.rglob("*.log"):
            assert "secret-marker" not in log.read_text()
        denied = start("192.0.2.10")
        assert call(denied, path, **{"X-Forwarded-For": "192.0.2.10", "X-Real-IP": "192.0.2.10"}) == 403
        assert call(start("127.0.0.1", tls=False), path) == 403
        assert len(seen) == before
    finally:
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=5)
        server.shutdown()
        server.server_close()
