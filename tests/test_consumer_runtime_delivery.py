"""真实nginx验证Dify回调不落入替换凭据的App代理。"""

import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from aiops_diagnostics.gateway_config import GatewayServerSettings


@pytest.mark.parametrize("managed", [False, True])
def test_remote_http_dify_is_refused_before_serving(tmp_path, managed):
    config = tmp_path / "production.env"
    config.write_text("# isolated\n")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
        execution_environment="env-test",
        consumer_chatflow_enabled=True,
        dify_chat_base_url="http://dify.invalid:10008",
        chat_route_bindings_file=str(tmp_path / "gateway.db") if managed else "",
    )
    with pytest.raises(ValueError, match="remote gateway URL must use HTTPS"):
        settings.validate()
    settings.dify_chat_base_url = "https://dify.example.invalid"
    settings.validate()


@pytest.mark.parametrize(
    "path", ["consumer-knowledge", "consumer-data/knowledge", "consumer-data/status", "consumer-data/billing"]
)
def test_callback_preserves_the_grant_and_rejects_other_methods(tmp_path, path):
    if shutil.which("nginx") is None:
        pytest.skip("真实nginx不可用")
    sockets = [socket.socket(), socket.socket()]
    for item in sockets:
        item.bind(("127.0.0.1", 0))
    front, upstream = [item.getsockname()[1] for item in sockets]
    snippet = (Path(__file__).parents[1] / "ops/nginx/dify-consumer-callback.conf").read_text()
    snippet = snippet.replace("http://172.18.0.1:8788", f"http://127.0.0.1:{upstream}")
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"daemon off; error_log {tmp_path / 'error.log'}; pid {tmp_path / 'nginx.pid'};\n"
        "events {} http { access_log off;\n"
        f"server {{ listen 127.0.0.1:{upstream}; location / {{ return 200 "
        "'$http_authorization|$http_x_third_session|$http_x_business_entry|$http_x_aiops_source_key'; } }\n"
        f"server {{ listen 127.0.0.1:{front}; {snippet}\n"
        "location /v1/ { return 418 'APP-FALLBACK'; } } }\n"
    )
    subprocess.run(["nginx", "-t", "-c", str(config)], check=True, capture_output=True)
    for item in sockets:
        item.close()
    process = subprocess.Popen(["nginx", "-c", str(config)])
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", front), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        base = f"http://127.0.0.1:{front}/v1/internal/{path}"
        headers = {
            "Authorization": "Bearer one-request-grant",
            "X-Third-Session": "spoof",
            "X-Business-Entry": "operator",
            "X-AIOps-Source-Key": "spoof",
        }
        with urlopen(Request(base, data=b"no", headers=headers), timeout=3) as response:
            assert response.read() == b"Bearer one-request-grant|||"
            assert response.headers["Cache-Control"] == "no-store"
        for method, suffix, status in [("GET", "", 405), ("PUT", "", 405), ("POST", "/extra", 418)]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + suffix, method=method), timeout=3)
            assert error.value.code == status
    finally:
        process.terminate()
        process.wait(timeout=5)
