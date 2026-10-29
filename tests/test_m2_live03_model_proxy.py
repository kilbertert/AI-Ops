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
sys.path.insert(0, str(REPO / "services/model-proxy"))

import model_proxy  # noqa: E402

PAYLOAD = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    extra_headers: list[tuple[str, str]] = []
    # 供应商实际出现过的形状，外加两种「帧说一套、连接做另一套」的坏形状：
    # 定长 JSON / chunked / TE+CL 同时出现 / 定长提前截断 / chunked 缺终止块。
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
        mode = type(self).mode
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        for name, value in type(self).extra_headers:
            self.send_header(name, value)
        if mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(PAYLOAD), PAYLOAD))
        elif mode == "both":
            # 矛盾长度：RFC 9112 §6.3 说这是可能的一次响应拆分/走私尝试。
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(PAYLOAD), PAYLOAD))
        elif mode == "duplicate-length-same":
            # RFC 9112 §6.3 rule 5：重复但相同的 CL 可以按单个值处理。
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif mode == "duplicate-length-conflict":
            self.send_header("Content-Length", "1")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif mode == "truncated-length":
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD[: len(PAYLOAD) // 2])
            self.close_connection = True
        elif mode == "truncated-chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(PAYLOAD), PAYLOAD))  # 缺终止块
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)


@pytest.fixture
def upstream_port():
    _Upstream.seen = []
    _Upstream.extra_headers = []
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


def _raw_post(port: int, path: str = "/v1/chat/completions") -> tuple[int, dict[str, str], bytes, bool, bool]:
    """严格客户端：按 HTTP 帧读完整响应，并要求 TLS 以 close_notify 收尾。

    `suppress_ragged_eofs=False` 是关键 —— 默认的 `True` 会把「没有 close_notify
    的 EOF」静默当成正常结束，而那正是 41 真实链路上 curl 56 的成因：body 收全了，
    连接却是被硬断开的。

    返回 `(status, headers, body, complete, clean_eof)`。`complete=False` 表示帧内
    声明的长度/终止块没有兑现 —— 调用方据此断言「残缺的上游没有被包装成成功」。
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

        complete = True
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while not payload.endswith(b"0\r\n\r\n"):
                chunk = tls.recv(65536)
                if not chunk:
                    complete = False
                    break
                payload += chunk
            if payload.endswith(b"0\r\n\r\n"):
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
                complete = False
        else:
            length = int(headers.get("content-length", "0"))
            while len(payload) < length:
                chunk = tls.recv(65536)
                if not chunk:
                    complete = False
                    break
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
        return status, headers, payload, complete, clean_eof


def test_upstream_connection_options_are_removed_before_forwarding(tmp_path, upstream_port):
    """RFC 9110 §7.6.1：重复 Connection 的选项均只属于当前一跳。"""
    _Upstream.extra_headers = [
        ("Connection", "keep-alive, X-First-Hop"),
        ("Connection", " x-SeCoNd-HoP "),
        ("X-First-Hop", "hop-only"),
        ("X-Second-Hop", "hop-only"),
        ("Keep-Alive", "timeout=5"),
        ("TE", "trailers"),
        ("Upgrade", "h2c"),
        ("X-End-To-End", "preserved"),
    ]
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, clean_eof = _raw_post(port)
    assert not ({"x-first-hop", "x-second-hop", "keep-alive", "te", "upgrade"} & headers.keys())
    assert headers["x-end-to-end"] == "preserved"
    assert headers["connection"] == "close"
    assert status == 200 and payload == PAYLOAD and complete and clean_eof


def test_a_content_length_response_ends_with_a_clean_tls_close(tmp_path, upstream_port):
    """真实链路回归：200 + 完整 body，但连接没有 close_notify ⇒ curl 56。

    修复前这条会红：客户端收到 `unexpected eof while reading`。
    """
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, clean_eof = _raw_post(port)
    assert status == 200
    assert payload == PAYLOAD
    assert headers["content-length"] == str(len(payload)), "有长度就必须按长度声明，别让客户端靠断开猜结束"
    assert complete
    assert clean_eof, "TLS 连接没有以 close_notify 收尾（严格客户端会读成 unexpected eof）"


def test_a_chunked_upstream_response_is_reframed_consistently(tmp_path, upstream_port):
    """上游 chunked、下游必须给出自洽帧：要么有长度，要么是合法 chunked。"""
    _Upstream.mode = "chunked"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, clean_eof = _raw_post(port)
    assert status == 200
    assert payload == PAYLOAD
    assert not (headers.get("content-length") and headers.get("transfer-encoding")), (
        "不能同时声明 Content-Length 与 Transfer-Encoding"
    )
    assert complete
    assert clean_eof


def test_an_upstream_error_status_keeps_its_body_and_framing(tmp_path, upstream_port):
    _Upstream.status = 400
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, clean_eof = _raw_post(port)
    assert status == 400, "错误响应必须保留上游状态码，不能被改写成 200/502"
    assert json.loads(payload)["choices"], "错误响应体也要完整送达"
    assert headers["content-length"] == str(len(payload))
    assert complete
    assert clean_eof


def test_ambiguous_upstream_framing_is_refused_not_forwarded(tmp_path, upstream_port):
    """RFC 9112 §6.3：TE 与 CL 同现时长度有歧义，转发前必须消歧，不能照抄 CL。

    修复前这条会红：代理按 `getheader("Content-Length")` 下发 CL=1，curl 退出 0
    却只拿到 `{`，把一个完整的模型响应截成了语法不完整的片段。
    """
    _Upstream.mode = "both"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, clean_eof = _raw_post(port)
    assert status == 502, "歧义长度的上游响应不能被当作正常响应转发"
    assert json.loads(payload)["error"]["code"] == "proxy.upstream_response_ambiguous"
    assert complete
    assert clean_eof


def test_ambiguous_upstream_framing_fails_curl_instead_of_returning_partial_json(tmp_path, upstream_port):
    """同一个缺陷的可执行形态：curl 不能再「退出 0 + 半个 JSON」。"""
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl 不在 PATH 上")
    _Upstream.mode = "both"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    code, stderr, body = _curl(port, with_status=False)
    assert code == 0, f"curl 退出 {code}：{stderr}"
    assert json.loads(body)["error"]["code"] == "proxy.upstream_response_ambiguous"
    assert body != PAYLOAD, "完整上游 body 不应以 CL=1 被截断后当成成功返回"


def test_repeated_identical_content_length_is_accepted(tmp_path, upstream_port):
    """RFC 9112 §6.3 rule 5：重复但相同的 CL 可以按单个值处理。"""
    _Upstream.mode = "duplicate-length-same"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, headers, payload, complete, _ = _raw_post(port)
    assert status == 200
    assert headers["content-length"] == str(len(PAYLOAD))
    assert payload == PAYLOAD
    assert complete


def test_conflicting_repeated_content_length_is_refused(tmp_path, upstream_port):
    """重复且不一致的 CL 不可判 → 拒绝，而不是挑一个转发。"""
    _Upstream.mode = "duplicate-length-conflict"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    status, _, payload, _, _ = _raw_post(port)
    assert status == 502
    assert json.loads(payload)["error"]["code"] == "proxy.upstream_response_ambiguous"


def test_a_truncated_content_length_upstream_is_not_reported_as_complete(tmp_path, upstream_port):
    """上游声明 N、只给半个 body：下游必须能看出「不完整」。"""
    _Upstream.mode = "truncated-length"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    _, headers, payload, complete, _ = _raw_post(port)
    assert headers["content-length"] == str(len(PAYLOAD)), "长度必须按上游声明转达"
    assert len(payload) < len(PAYLOAD), "上游提前断流时不应凭空补齐 body"
    assert not complete, "残缺的上游 body 不能被包装成干净完整成功"


def test_a_truncated_chunked_upstream_is_not_reported_as_complete(tmp_path, upstream_port):
    """上游 chunked 缺终止块：下游不能替它补发 `0\\r\\n\\r\\n`。"""
    _Upstream.mode = "truncated-chunked"
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    _, headers, _, complete, _ = _raw_post(port)
    assert headers.get("transfer-encoding") == "chunked"
    assert not complete, "上游没给终止块时，代理不能补一个干净结束"


def _curl(port: int, *, with_status: bool = True) -> tuple[int, str, bytes]:
    """用 curl 发一次真实请求；返回 `(退出码, stderr, stdout)`。"""
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl 不在 PATH 上")
    args = [
        curl,
        "-sS",
        "-k",
        "--http1.1",
        "-H",
        "Authorization: Bearer proxy-token",
        "-H",
        "Content-Type: application/json",
        "--data",
        '{"model": "vendor-model", "messages": []}',
    ]
    if with_status:
        args += ["-w", "%{http_code}"]
    result = subprocess.run(
        [*args, f"https://127.0.0.1:{port}/v1/chat/completions"],
        capture_output=True,
        timeout=30,
    )
    return result.returncode, result.stderr.decode(), result.stdout


def test_curl_completes_cleanly_against_a_non_streaming_completion(tmp_path, upstream_port):
    """41 上的原症状复现：curl 必须退出 0，而不是 56。"""
    port = _start(tmp_path, f"http://127.0.0.1:{upstream_port}")
    code, stderr, stdout = _curl(port)
    assert code == 0, f"curl 退出 {code}：{stderr}"
    assert stdout == PAYLOAD + b"200", "body 必须逐字节完整，不能只拿到前缀"
