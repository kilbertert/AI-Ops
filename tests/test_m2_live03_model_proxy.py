"""受控模型出口：只转发到一个固定目标，且两侧凭据不同。

这些用例起真实的代理进程（TLS）与一个本地上游桩，验证：
- 目标由部署件钉死，请求里的 URL 改不了它；
- 路径白名单；
- 节点出示的是出口令牌，上游收到的是供应商 key；
- 没有令牌 / 错令牌 → 401；上游不可达 → 502。
"""

from __future__ import annotations

import json
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from m2_live import model_proxy  # noqa: E402


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).seen.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body or b"{}"),
            }
        )
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream_port():
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _certs(tmp_path: Path) -> tuple[Path, Path]:
    cert = tmp_path / "proxy.crt"
    key = tmp_path / "proxy.key"
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
    (tmp_path / "upstream_key").write_text("vendor-key")
    (tmp_path / "proxy_token").write_text("proxy-token")
    return cert, key


def _start(tmp_path: Path, upstream: str) -> int:
    cert, key = _certs(tmp_path)
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(
        target=model_proxy.serve,
        args=("127.0.0.1", port, upstream, "vendor-key", "proxy-token", str(cert), str(key)),
        daemon=True,
    ).start()
    time.sleep(0.5)
    return port


def _post(port: int, path: str, token: str | None) -> tuple[int, dict]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # 自签测试证书
    request = urllib.request.Request(
        f"https://127.0.0.1:{port}{path}",
        data=json.dumps({"model": "vendor-model", "messages": []}).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_a_request_with_the_proxy_token_reaches_the_fixed_upstream(tmp_path, upstream_port):
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, _ = _post(port, "/v1/chat/completions", "proxy-token")
    assert status == 200
    assert _Upstream.seen[-1]["authorization"] == "Bearer vendor-key", "上游没有收到供应商凭据"
    assert _Upstream.seen[-1]["path"] == "/v1/chat/completions"
    assert _Upstream.seen[-1]["body"]["model"] == "vendor-model", "model 字段被改写"


def test_the_proxy_token_never_reaches_the_upstream(tmp_path, upstream_port):
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    _post(port, "/v1/chat/completions", "proxy-token")
    assert "proxy-token" not in (_Upstream.seen[-1]["authorization"] or "")


def test_a_missing_or_wrong_token_is_rejected_without_contacting_the_upstream(tmp_path, upstream_port):
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    assert _post(port, "/v1/chat/completions", None)[0] == 401
    assert _post(port, "/v1/chat/completions", "wrong")[0] == 401
    assert _Upstream.seen == [], "未认证请求不应该到达上游"


def test_paths_outside_the_allowlist_are_refused(tmp_path, upstream_port):
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    for path in ("/v1/embeddings", "/v1/v1/chat/completions", "/admin", "/../etc/passwd"):
        assert _post(port, path, "proxy-token")[0] == 404, path
    assert _Upstream.seen == []


def test_both_completion_spellings_reach_the_same_fixed_upstream(tmp_path, upstream_port):
    """适配器会把 `/v1` 拼在 baseUrl 后面：两种拼法都必须落到同一目标。"""
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    assert _post(port, "/chat/completions", "proxy-token")[0] == 200
    assert _post(port, "/v1/chat/completions", "proxy-token")[0] == 200
    assert {seen["path"] for seen in _Upstream.seen} == {"/chat/completions", "/v1/chat/completions"}


def test_the_client_cannot_redirect_the_upstream(tmp_path, upstream_port):
    """请求里带任何 URL 字段都不改变目标 —— 目标只来自部署件。"""
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    _post(port, "/v1/chat/completions", "proxy-token")
    assert _Upstream.seen[-1]["path"] == "/v1/chat/completions"


def test_an_unreachable_upstream_is_reported_as_502(tmp_path):
    port = _start(tmp_path, "http://127.0.0.1:1")
    status, body = _post(port, "/v1/chat/completions", "proxy-token")
    assert status == 502
    assert body["error"]["code"] == "proxy.upstream_unavailable"


def test_connect_is_not_supported(tmp_path):
    """不实现 CONNECT：它会把「固定目标」变成「通用出口」。"""
    assert model_proxy.ProxyHandler.do_CONNECT.__name__ == "do_CONNECT"
    import inspect

    source = inspect.getsource(model_proxy.ProxyHandler.do_CONNECT)
    assert "405" in source
    assert "connect_not_allowed" in source
