"""真实 nginx 限定凭据交换来源；登录代理本体沿用既有独立密钥/会话测试。"""

import http.client
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_exchange_ingress_uses_tcp_peer_not_forwarded_headers(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx未安装，不能验证真实代理")
    seen = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"token":"synthetic"}')

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    template = Path(__file__).resolve().parents[2] / "ops/dify-login-proxy/operator-token.conf.template"
    location = template.read_text().replace("${AIOPS_OPERATOR_GATEWAY_SOURCE_IP}", "127.0.0.2")
    location = location.replace("127.0.0.1:10119", f"127.0.0.1:{upstream.server_port}")
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path}/nginx.pid; error_log stderr; events {{}} http {{ access_log off; "
        f"client_body_temp_path {tmp_path}; proxy_temp_path {tmp_path}; "
        f"server {{ listen 127.0.0.1:{port}; set_real_ip_from 127.0.0.0/8; real_ip_header X-Real-IP; "
        + location
        + " location / { return 404; } } }"
    )
    process = subprocess.Popen([nginx, "-c", str(config), "-g", "daemon off;"], stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert process.poll() is None
                time.sleep(0.02)

        def request(peer, method="POST", path="/__aiops/internal/operator-token", headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2, source_address=(peer, 0))
            try:
                c.request(
                    method,
                    path,
                    body=b'{"session":"synthetic"}',
                    headers={
                        "Authorization": "Bearer separate",
                        "Content-Type": "application/json",
                        **(headers or {}),
                    },
                )
                r = c.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            finally:
                c.close()

        denied = request("127.0.0.1", headers={"X-Real-IP": "127.0.0.2", "X-Forwarded-For": "127.0.0.2"})
        assert denied[0] == 403 and denied[1]["Cache-Control"] == "no-store" and not seen
        allowed = request("127.0.0.2", headers={"X-Real-IP": "203.0.113.8"})
        assert allowed[0] == 200 and allowed[1]["Cache-Control"] == "no-store"
        assert seen == [("/__aiops/internal/operator-token", "Bearer separate", b'{"session":"synthetic"}')]
        assert request("127.0.0.2", method="GET")[0] == 403
        assert request("127.0.0.2", path="/__aiops/internal/operator-token/extra")[0] == 404
        assert len(seen) == 1
    finally:
        process.terminate()
        process.communicate(timeout=5)
        upstream.shutdown()
        upstream.server_close()
