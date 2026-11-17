"""独立环境节点不获得任意外网；回调沿唯一工具声明且双段TLS校验。"""

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
import yaml

ROOT = Path(__file__).resolve().parents[2]
render = runpy.run_path(str(ROOT / "tools/content/render_node_callback.py"))["render_egress"]


@pytest.mark.parametrize(
    "url",
    [
        "http://target.example",
        "https://user@target.example",
        "https://target.example/path",
        "https://target.example?x=1",
        "https://target.example:0",
        "https://x;return.example",
        "https://target.example/",
    ],
)
def test_reject_non_origin(url):
    with pytest.raises(ValueError):
        render(url)


def test_environment_compose_keeps_shared_services_external():
    compose = yaml.safe_load((ROOT / "deploy/node-runtime/environment.compose.yaml").read_text())
    assert set(compose["services"]) == {"environment-node", "gateway-egress"}
    node = compose["services"]["environment-node"]
    assert set(node["networks"]) == {"trial", "modellink", "callback"}
    assert compose["networks"]["callback"]["internal"]
    assert all(compose["networks"][n]["external"] for n in ("trial", "modellink"))
    assert node["networks"]["trial"]["aliases"] == ["${AIOPS_ENV_NODE_HOST:?}"]
    assert node["environment"]["AIOPS_GATEWAY_BASE_URL"] == "https://gateway-egress:9443"
    assert all(not s.get("ports") and not s.get("privileged") for s in compose["services"].values())
    assert set(compose["services"]["gateway-egress"]["networks"]) == {"callback", "egress"}


def test_tls_gateway_egress_only_forwards_declared_tools(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("需要真实Nginx")
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

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            seen.append((self.path, dict(self.headers)))
            self.send_response(200 if self.headers.get("Authorization") == "Bearer fixture" else 401)
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    upstream.socket = tls.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    def start(wrong_name=False):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        folder = tmp_path / str(port)
        folder.mkdir()
        config = render(f"https://localhost:{upstream.server_port}")
        config = config.replace("listen 9443 ssl", f"listen 127.0.0.1:{port} ssl").replace(
            "/tmp/", str(folder) + "/"
        )
        config = (
            config.replace("/etc/aiops-tls/server.crt", str(cert))
            .replace("/etc/aiops-tls/server.key", str(key))
            .replace("/etc/ssl/certs/ca-certificates.crt", str(cert))
        )
        if wrong_name:
            config = config.replace("proxy_ssl_name localhost", "proxy_ssl_name mismatch.example")
        path = folder / "nginx.conf"
        path.write_text(config)
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

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))),
    )

    def call(base, path, method="POST", token="fixture"):
        req = urllib.request.Request(
            base + path,
            data=b"{}" if method == "POST" else None,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Host": "untrusted.example",
                "Cookie": "private",
                "tenant-id": "forged",
                "X-AI-Ops-Exec-Config": "manifest",
                "X-AI-Ops-Tool-Contract": "contract",
                "X-AI-Ops-Node-Build": "build",
            },
        )
        try:
            r = opener.open(req, timeout=3)
        except urllib.error.HTTPError as exc:
            r = exc
        with r:
            return r.status

    try:
        base = start()
        path = "/v1/internal/execution-node/capabilities"
        assert call(base, path) == 200
        assert seen[-1][1]["Host"] == f"localhost:{upstream.server_port}"
        assert seen[-1][1]["X-AI-Ops-Node-Build"] == "build"
        assert not {"cookie", "tenant-id"} & {h.lower() for h in seen[-1][1]}
        assert call(base, path, token="wrong") == 401
        from aiops_diagnostics.operator import operator_tools

        for tool in operator_tools.tools():
            prefix = "/v1/internal/operator-runs/run_fixture"
            assert call(base, prefix + f"/tools/{tool.name}/authorization") == 200
            assert seen[-1][1]["X-AI-Ops-Tool-Contract"] == "contract"
            assert seen[-1][1]["X-AI-Ops-Exec-Config"] == "manifest"
            route = tool.path_template
            for name in tool.inputs:
                route = route.replace("{" + name + "}", "fixture")
            assert call(base, prefix + "/cpms" + route + "?page=1", "GET") == 200
        before = len(seen)
        for p in ("/admin", "/v1/internal/operator-runs/run_fixture/cpms/unknown"):
            assert call(base, p) == 404
        for p in (
            "/v1/internal/../admin",
            "/v1/internal/execution-node/capabilities/../capabilities",
            "/v1/internal/operator-runs/run_fixture/cpms/chSite%2fpage",
            "/v1/internal/operator-runs/run_fixture/cpms/chSite/page/../page",
        ):
            assert call(base, p, "GET") == 400
        assert call(base, path, "GET") == 405
        assert call(base, "/v1/internal/operator-runs/run_fixture/cpms/chSite/page", "HEAD") == 405
        assert call(start(True), path) == 502
        assert len(seen) == before
    finally:
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
