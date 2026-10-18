"""受控模型出口：**只转发到一个固定目标**的最小代理。

存在的原因：执行节点在 M2-LIVE-03 里只接内部网络（它没有任意外联，这是刻意的边界），
而模型调用发生在节点进程里（`ClientChatExecutor` 在节点构造 ChatClient 并直接请求
模型端点）。合成栈把模型桩放在同一张内部网络上，掩盖了真实供应商不可达这件事。

本代理不是通用出口：

- **目标固定**：上游 URL 来自部署件（`M2LIVE_MODEL_UPSTREAM`），请求里的任何字段都
  不能改变它；不实现 CONNECT，也没有「按 URL 转发」。
- **路径白名单**：只接受 `/v1/chat/completions` 与 `/v1/models`；其余 404。
- **凭据分离**：真实供应商 key 只在代理里（0600 文件）；节点只拿到代理专用令牌。
  两者都只从文件读，不进命令行、不进日志。
- **TLS 双向校验**：节点→代理由本栈 CA 签发（SAN=model-proxy），代理→供应商用系统
  公开 CA 校验。任何一侧都不提供「关闭校验」的开关。
- **响应帧化自洽、不缓冲**：上游给了 `Content-Length` 就原样转达（非流式客户端凭它
  判定结束）；没有长度（流式/HTTP1.0）就按 `chunked` 重写；连接以 TLS `close_notify`
  收尾。没有长度上限就谈不上缓冲 —— body 是按块转发的，不整段读进内存。
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import json
import os
import ssl
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Spring AI 的 OpenAI 兼容适配器默认把 completionsPath 设为 `/v1/chat/completions`，
# 但实测该版本（openai-java 4.39.1）实际请求的是 `/chat/completions` —— 取决于
# baseUrl 是否自带 `/v1`。两种拼法都只在同一组固定端点内，所以两个都白名单；
# 不引入「任意路径转发」。
ALLOWED_PATHS = frozenset({"/chat/completions", "/v1/chat/completions", "/v1/models", "/models"})
MAX_BODY = 2 * 1024 * 1024


def log(message: str) -> None:
    print(f"[model-proxy] {message}", flush=True)


def read_secret(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "m2-model-proxy"

    # 由 serve() 注入
    upstream: urllib.parse.SplitResult
    upstream_key: str
    proxy_token: str

    def log_message(self, *_args: object) -> None:  # 不打印请求行：可能带令牌
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def finish(self) -> None:
        """TLS 收尾必须发 close_notify，否则客户端看到的是「被截断的」响应。

        只关 TCP 而不做 TLS shutdown，会让严格客户端把「body 已完整收到」读成
        `unexpected eof` —— 41 真实链路就是这样：供应商 200 与模型响应都到了，
        curl 却以 56 退出，探针因此判失败。close-delimited 的响应尤其依赖这个
        干净的 EOF 才能收尾。
        """
        with contextlib.suppress(OSError):
            self.wfile.flush()
        connection = self.connection
        if isinstance(connection, ssl.SSLSocket):
            with contextlib.suppress(OSError, ValueError):
                connection.settimeout(2)
                connection.unwrap()
        super().finish()

    def do_CONNECT(self) -> None:  # noqa: N802 - 明确不支持
        self._json(405, {"error": {"code": "proxy.connect_not_allowed"}})

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/v1/models", "/models"}:
            self._json(404, {"error": {"code": "proxy.path_not_allowed"}})
            return
        self._forward(b"")

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ALLOWED_PATHS:
            log(f"path refused: {self.path}")
            self._json(404, {"error": {"code": "proxy.path_not_allowed"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": {"code": "proxy.body_too_large"}})
            return
        self._forward(self.rfile.read(length) if length else b"")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        presented = header.removeprefix("Bearer ").strip()
        return bool(presented) and hmac.compare_digest(presented, self.proxy_token)

    def _forward(self, body: bytes) -> None:
        if not self._authorized():
            self._json(401, {"error": {"code": "proxy.token_required"}})
            return
        context = ssl.create_default_context()  # 系统公开 CA；不可关闭
        target = self.upstream
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = http.client.HTTPSConnection(
                target.hostname, target.port or 443, timeout=60, context=context
            )
        else:  # 合成模式：上游在栈内，仍是同一个固定目标
            connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=60)
        # 上游只给**源**；前缀是部署配置里的一个固定值，路径是客户端请求路径。
        # 两者拼接是确定性的：`base_path + request_path`。不猜测、不去重 ——
        # 若配置成 `/v1` 而客户端也发 `/v1/...`，结果会明显是 `/v1/v1/...` 并被上游 404，
        # 那时是配置错，不是运行时该替它修的事。
        path = self.upstream_base_path + self.path
        headers = {
            "Authorization": f"Bearer {self.upstream_key}",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "*/*"),
        }
        try:
            connection.request(self.command, path, body=body or None, headers=headers)
            response = connection.getresponse()
        except OSError as exc:
            log(f"upstream unreachable: {type(exc).__name__}")
            self._json(502, {"error": {"code": "proxy.upstream_unavailable"}})
            return
        self.send_response(response.status)  # 已发出本进程的 Server / Date
        length = response.getheader("Content-Length")
        for name, value in response.getheaders():
            # 逐跳头由本进程重新表达；Server/Date 是单例，原样再发一次就成了重复头。
            if name.lower() in {
                "transfer-encoding",
                "connection",
                "content-length",
                "server",
                "date",
                "trailer",
            }:
                continue
            self.send_header(name, value)
        # 帧化由本进程重写：上游的 chunked 已被 http.client 解开，原样透传那两个
        # 头会让客户端看到与 body 不符的声明。有长度就透传长度（非流式客户端不必
        # 靠连接关闭判定结束）；没有长度就按 chunked 重写。不缓冲 body —— 模型
        # 调用可能是流式的，缓冲会同时破坏流式语义和内存上限。
        bodyless = self.command == "HEAD" or response.status in (204, 304)
        chunked = length is None and not bodyless
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif length is not None:
            self.send_header("Content-Length", length)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            connection.close()
            self.close_connection = True


def serve(
    listen_host: int | str,
    listen_port: int,
    upstream: str,
    upstream_key: str,
    proxy_token: str,
    certfile: str,
    keyfile: str,
    synthetic: bool = False,
    upstream_base_path: str = "",
) -> None:
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("M2LIVE_MODEL_UPSTREAM 必须是完整的 http/https URL")
    if parsed.scheme != "https" and not synthetic:
        raise SystemExit("正式模式的 M2LIVE_MODEL_UPSTREAM 必须使用 HTTPS")
    if parsed.path not in ("", "/"):
        raise SystemExit("M2LIVE_MODEL_UPSTREAM 只能是源（不要带路径前缀）；前缀由 --upstream-base-path 给出")
    # 供应商的基路径（通常是 `/v1`）是**部署配置**，不是从请求里学的。
    # 适配器把 `baseUrl` 之后的路径算作 `/chat/completions`（实测 openai-java 4.39.1），
    # 所以要落到供应商的 `/v1/chat/completions`，前缀必须在这里显式拼上。
    base_path = upstream_base_path.strip()
    if base_path and (not base_path.startswith("/") or ".." in base_path or "?" in base_path):
        raise SystemExit("--upstream-base-path 必须是以 / 开头的固定前缀")
    ProxyHandler.upstream = parsed
    ProxyHandler.upstream_base_path = base_path.rstrip("/")
    ProxyHandler.upstream_key = upstream_key
    ProxyHandler.proxy_token = proxy_token
    # 故意**不**记录上游主机名之外的任何东西：也绝不记录 key。
    log(f"upstream scheme={parsed.scheme} host={parsed.hostname}")
    httpd = ThreadingHTTPServer((listen_host, listen_port), ProxyHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    log(f"listening on {listen_host}:{listen_port} (TLS)")
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=9101)
    parser.add_argument("--upstream", default=os.environ.get("M2LIVE_MODEL_UPSTREAM", ""))
    parser.add_argument("--upstream-key-file", default="/etc/m2live/upstream_api_key")
    parser.add_argument("--proxy-token-file", default="/etc/m2live/proxy_token")
    parser.add_argument("--upstream-base-path", default=os.environ.get("M2LIVE_MODEL_UPSTREAM_BASE_PATH", ""))
    parser.add_argument("--cert", default="/etc/m2live-tls/model-proxy.crt")
    parser.add_argument("--key", default="/etc/m2live-tls/model-proxy.key")
    args = parser.parse_args()

    if not args.upstream:
        print("缺少 --upstream / M2LIVE_MODEL_UPSTREAM", file=sys.stderr)
        return 2
    serve(
        args.listen_host,
        args.listen_port,
        args.upstream,
        read_secret(args.upstream_key_file),
        read_secret(args.proxy_token_file),
        args.cert,
        args.key,
        synthetic=os.environ.get("M2LIVE_SYNTH_MODE") == "1",
        upstream_base_path=args.upstream_base_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
