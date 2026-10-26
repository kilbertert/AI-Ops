"""正式配置边界，以及真正 nginx 的路径/认证透传。"""

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/node-runtime"


def test_runtime_is_internal_and_keeps_credentials_separate():
    config = yaml.safe_load((DEPLOY / "compose.yaml").read_text())
    services = config["services"]
    assert set(services) == {"store", "platform", "node", "model-proxy"}
    for name in ("node", "platform", "store"):
        assert all(config["networks"][n]["internal"] for n in services[name]["networks"])
    assert services["platform"]["networks"]["trial"]["ipv4_address"] == "${AIOPS_NODE_PLATFORM_ADDRESS:?}"
    assert all(not s.get("ports") for s in services.values())
    for name in ("node", "platform", "model-proxy"):
        assert services[name]["user"] == "${AIOPS_NODE_UID:?}:${AIOPS_NODE_GID:?}"
    assert "--aiops.node.managed-publication=true" in services["node"]["command"]
    assert "upstream_api_key" not in json.dumps(services["node"])
    assert all("/etc/aiops-41" not in v for s in services.values() for v in s.get("volumes", []))
    assert services["model-proxy"]["volumes"] == [
        "${AIOPS_NODE_CONFIG:?}/model-proxy:/etc/m2live:ro",
        "${AIOPS_NODE_CONFIG:?}/tls/proxy:/etc/m2live-tls:ro",
    ]


def test_nginx_forwards_only_node_posts_with_original_credentials(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("nginx 未安装；该环境不证明实际代理行为")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization"), None, b""))
            self.send_response(204)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            seen.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("X-AI-Ops-Exec-Config"),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    text = (DEPLOY / "gateway-callback.conf.template").read_text()
    for key, value in {
        "AIOPS_NODE_BRIDGE": "127.0.0.1",
        "AIOPS_NODE_STATE": str(tmp_path),
        "AIOPS_PRODUCTION_GATEWAY": f"http://127.0.0.1:{upstream.server_port}",
    }.items():
        text = text.replace("${" + key + "}", value)
    config = tmp_path / "nginx.conf"
    config.write_text(text.replace(":8788;", f":{port};"))
    proc = subprocess.Popen(
        [nginx, "-e", "stderr", "-c", str(config), "-g", "daemon off;"], stderr=subprocess.PIPE
    )
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert proc.poll() is None, proc.stderr.read().decode()
                time.sleep(0.02)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for endpoint in ("claims", "completions", "execution-config"):
            path = f"/v1/internal/charging-status/{endpoint}/run_test"
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=b"{}",
                headers={"Authorization": "Bearer synthetic", "X-AI-Ops-Exec-Config": "digest"},
            )
            with opener.open(req, timeout=2) as res:
                assert res.status == 204
            assert seen[-1] == (path, "Bearer synthetic", "digest", b"{}")
        for path, method in [
            ("/v1/assistant/questions", "POST"),
            ("/v1/internal/charging-status/claims/run_test", "GET"),
            ("/v1/internal/charging-status/claims/not-a-run", "POST"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(
                    urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=2
                )
            assert error.value.code in (403, 404)
        assert len(seen) == 3
        root = "/v1/internal/operator-runs/run_test"
        for tail in (
            "completions",
            "tools/cpms_order_detail/authorization",
            "tools/cpms_charging_samples/evidence",
        ):
            path = root + "/" + tail
            with opener.open(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=b"{}",
                    headers={"Authorization": "Bearer node-token", "X-AI-Ops-Exec-Config": "manifest"},
                ),
                timeout=2,
            ) as res:
                assert res.status == 204
            assert seen[-1] == (path, "Bearer node-token", "manifest", b"{}")
        path = root + "/cpms/iotChargingDevice/getChargingMonitor/ORDER-1?current=1&size=50"
        with opener.open(
            urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer task-grant"}
            ),
            timeout=2,
        ) as res:
            assert res.status == 204
        assert seen[-1] == (path, "Bearer task-grant", None, b"")
        for path, method in [
            (root + "/tools/shell/authorization", "POST"),
            (root + "/cpms/chOrderInfo/stopCharging/ORDER-1", "GET"),
            (root + "/cpms/chOrderInfo/getOrderInfoDetail/ORDER-1", "POST"),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(
                    urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=2
                )
            assert error.value.code in (403, 404)
        assert len(seen) == 7
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
        upstream.shutdown()
