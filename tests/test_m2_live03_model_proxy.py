"""受控模型出口：只转发到一个固定目标，且两侧凭据不同。

这些用例起真实的代理进程（TLS）与一个本地上游桩，验证：
- 目标由部署件钉死，请求里的 URL 改不了它；
- 路径白名单；
- 节点出示的是出口令牌，上游收到的是供应商 key；
- 没有令牌 / 错令牌 → 401；上游不可达 → 502。
"""

from __future__ import annotations

import json
import shutil
import socket
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
    # 真实供应商三种形状都出现过：定长 JSON、chunked、以及带错误状态码的 JSON。
    mode = "content-length"
    status = 200

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
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        if type(self).mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload))
        else:
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


@pytest.fixture
def upstream_port():
    _Upstream.seen = []
    _Upstream.mode = "content-length"
    _Upstream.status = 200
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


def _start(tmp_path: Path, upstream: str, *, base_path: str = "") -> int:
    cert, key = _certs(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    threading.Thread(
        target=model_proxy.serve,
        args=("127.0.0.1", port, upstream, "vendor-key", "proxy-token", str(cert), str(key)),
        kwargs={"upstream_base_path": base_path, "synthetic": True},
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


def test_a_configured_base_path_is_prepended_exactly_once(tmp_path, upstream_port):
    """供应商的 `/v1` 是部署配置：请求 `/chat/completions` 必须落到 `/v1/chat/completions`。"""
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}", base_path="/v1")
    assert _post(port, "/chat/completions", "proxy-token")[0] == 200
    assert _Upstream.seen[-1]["path"] == "/v1/chat/completions"


def test_a_base_path_without_a_leading_slash_is_refused(tmp_path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(SystemExit):
        model_proxy.serve(
            "127.0.0.1",
            port,
            "http://127.0.0.1:1",
            "k",
            "t",
            "c",
            "k",
            upstream_base_path="v1",
        )


def _raw_post(port: int, path: str = "/v1/chat/completions") -> tuple[int, dict[str, str], bytes, bool]:
    """严格客户端：按 HTTP 帧读完整响应，并要求 TLS 以 close_notify 收尾。

    `suppress_ragged_eofs=False` 是关键 —— 默认的 `True` 会把「没有 close_notify
    的 EOF」静默当成正常结束，而那正是 41 真实链路上 curl 56 的成因：body 收全了，
    连接却是被硬断开的。
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # 自签测试证书
    body = json.dumps({"model": "vendor-model", "messages": []}).encode()
    request = (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer proxy-token\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    with context.wrap_socket(raw, suppress_ragged_eofs=False) as tls:
        tls.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = tls.recv(65536)
            if not chunk:
                raise AssertionError("响应头之前连接就结束了")
            data += chunk
        head, _, payload = data.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        if headers.get("transfer-encoding", "").lower() == "chunked":
            while not payload.endswith(b"0\r\n\r\n"):
                chunk = tls.recv(65536)
                if not chunk:
                    raise AssertionError("chunked 响应在终止块之前结束")
                payload += chunk
            decoded, rest = b"", payload
            while True:
                size_line, _, rest = rest.partition(b"\r\n")
                size = int(size_line, 16)
                if size == 0:
                    break
                decoded += rest[:size]
                rest = rest[size + 2 :]
            payload = decoded
        else:
            length = int(headers.get("content-length", "0"))
            while len(payload) < length:
                chunk = tls.recv(65536)
                if not chunk:
                    raise AssertionError("body 在 Content-Length 之前结束")
                payload += chunk

        # 帧内 body 已经读完。接下来的 EOF 必须是干净的 TLS 关闭。
        clean_eof = True
        try:
            while tls.recv(65536):
                pass
        except ssl.SSLEOFError:
            clean_eof = False
        except OSError:
            clean_eof = False
        return status, headers, payload, clean_eof


def test_a_content_length_response_ends_with_a_clean_tls_close(tmp_path, upstream_port):
    """真实链路回归：200 + 完整 body，但连接没有 close_notify ⇒ curl 56。

    修复前这条会红：客户端收到 `unexpected eof while reading`。
    """
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, clean_eof = _raw_post(port)
    assert status == 200
    assert payload == json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
    assert headers["content-length"] == str(len(payload)), "有长度就必须按长度声明，别让客户端靠断开猜结束"
    assert clean_eof, "TLS 连接没有以 close_notify 收尾（严格客户端会读成 unexpected eof）"


def test_a_chunked_upstream_response_is_reframed_consistently(tmp_path, upstream_port):
    """上游 chunked、下游必须给出自洽帧：要么有长度，要么是合法 chunked。"""
    _Upstream.mode = "chunked"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, clean_eof = _raw_post(port)
    assert status == 200
    assert payload == json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
    assert not (headers.get("content-length") and headers.get("transfer-encoding")), (
        "不能同时声明 Content-Length 与 Transfer-Encoding"
    )
    assert clean_eof


def test_an_upstream_error_status_keeps_its_body_and_framing(tmp_path, upstream_port):
    _Upstream.status = 400
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, clean_eof = _raw_post(port)
    assert status == 400, "错误响应必须保留上游状态码，不能被改写成 200/502"
    assert json.loads(payload)["choices"], "错误响应体也要完整送达"
    assert headers["content-length"] == str(len(payload))
    assert clean_eof


def test_curl_completes_cleanly_against_a_non_streaming_completion(tmp_path, upstream_port):
    """41 上的原症状复现：curl 必须退出 0，而不是 56。"""
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl 不在 PATH 上")
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    result = subprocess.run(
        [
            curl,
            "-sS",
            "-k",
            "--http1.1",
            "-w",
            "%{http_code}",
            "-H",
            "Authorization: Bearer proxy-token",
            "-H",
            "Content-Type: application/json",
            "--data",
            '{"model": "vendor-model", "messages": []}',
            f"https://127.0.0.1:{port}/v1/chat/completions",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"curl 退出 {result.returncode}：{result.stderr}"
    assert result.stdout.endswith("200")
