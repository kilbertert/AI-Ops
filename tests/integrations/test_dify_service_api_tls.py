"""真实nginx/TLS证明备用入口不暴露控制台、来源范围与应用鉴权保留。"""

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

pytestmark = pytest.mark.nginx


def test_tls_source_scope_routes_and_streaming(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx未安装")
    root = Path(__file__).resolve().parents[2]
    template = (root / "ops/dify-console-bridge/dify-service-api-TLS.conf").read_text()
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
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            status = 200 if self.headers.get("Authorization") == "Bearer valid-fixture" else 401
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"event":"message_end"}\n\n')

        do_POST = do_GET

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    processes = []

    def start(source):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        folder = tmp_path / str(port)
        folder.mkdir()
        sources = folder / "sources.conf"
        sources.write_text(f"allow {source};\n")
        config = template.replace("listen 10443 ssl", f"listen 127.0.0.1:{port} ssl")
        config = config.replace("server_name dify.ranlei.work", "server_name localhost")
        config = config.replace("/www/server/panel/vhost/cert/dify.ranlei.work/fullchain.pem", str(cert))
        config = config.replace("/www/server/panel/vhost/cert/dify.ranlei.work/privkey.pem", str(key))
        config = config.replace("/etc/aiops-dify-api/allowed-sources.conf", str(sources))
        config = config.replace("/www/wwwlogs/aiops-dify-service-api", str(folder / "api"))
        config = config.replace("127.0.0.1:10018", f"127.0.0.1:{upstream.server_port}")
        path = folder / "nginx.conf"
        path.write_text(f"pid {folder}/pid; error_log {folder}/error.log; events {{}} http {{\n{config}\n}}")
        proc = subprocess.Popen(
            [nginx, "-e", "stderr", "-c", str(path), "-g", "daemon off;"], stderr=subprocess.PIPE
        )
        processes.append(proc)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return f"https://localhost:{port}"
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                time.sleep(0.02)
        pytest.fail("nginx未启动")

    def call(base, path, token="", method="GET", **headers):
        req = urllib.request.Request(base + path, headers={"Authorization": token, **headers}, method=method)
        context = ssl.create_default_context(cafile=str(cert))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            return response.status, response.read()

    try:
        base = start("127.0.0.1")
        assert call(base, "/v1/info")[0] == 401
        assert b"message_end" in call(base, "/v1/chat-messages", "Bearer valid-fixture")[1]
        for path in [
            "/v1/files/upload",
            "/v1/chat-messages/task-1/stop",
            "/v1/workflows/version-1/run",
            "/v1/workflows/tasks/task-1/stop",
        ]:
            assert call(base, path, "Bearer valid-fixture", method="POST")[0] == 200
        before = len(seen)
        for path in [
            "/apps",
            "/console/api/apps",
            "/inner/api/apps",
            "/v1/files/arbitrary",
            "/v1/chat-messages/extra",
        ]:
            assert call(base, path, "Bearer valid-fixture")[0] == 404
        assert call(base, "/v1/info", "Bearer valid-fixture", method="DELETE")[0] == 403
        assert len(seen) == before
        denied = start("192.0.2.1")
        assert call(denied, "/v1/info", "Bearer valid-fixture", **{"X-Forwarded-For": "192.0.2.1"})[0] == 403
        assert len(seen) == before
        assert call(denied, "/v1/info?private_query_marker=secret", "Bearer valid-fixture")[0] == 403
        for log in tmp_path.rglob("*.log"):
            assert "private_query_marker" not in log.read_text()
    finally:
        for p in processes:
            p.terminate()
            p.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
