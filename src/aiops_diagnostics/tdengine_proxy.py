from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

GUN_COLUMNS = (
    "_ts, `txSerialNo`, status, `isReturn`, `isInsert`, `outputVoltage`, `outputCurrent`, power, "
    "`chargingTime`, `chargingElectricityQuantity`, soc, temperature, `batteryMaxTemperature`, "
    "`batteryMinTemperature`, `errorCode`, `errorReason`, `meterNow`"
)
COMM_COLUMNS = "_ts, direction, code, decoded"
SAFE_VALUE = r"[A-Za-z0-9_.:-]{1,128}"
TIME_VALUE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"

GUN_QUERY = re.compile(
    rf"^SELECT {re.escape(GUN_COLUMNS)} FROM `charging-gun_property` "
    rf"WHERE device='(?P<device>{SAFE_VALUE})' AND _ts>='(?P<start>{TIME_VALUE})' "
    rf"AND _ts<='(?P<end>{TIME_VALUE})'"
    rf"(?: AND `txSerialNo`='(?P<tx>{SAFE_VALUE})')? ORDER BY _ts ASC LIMIT (?P<limit>\d+)$"
)
COMM_QUERY = re.compile(
    rf"^SELECT {re.escape(COMM_COLUMNS)} FROM `charging-pile_comm` "
    rf"WHERE device='(?P<device>{SAFE_VALUE})' AND _ts>='(?P<start>{TIME_VALUE})' "
    rf"AND _ts<='(?P<end>{TIME_VALUE})' ORDER BY _ts ASC LIMIT (?P<limit>\d+)$"
)
SAFE_DATABASE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProxyConfigError(ValueError):
    """The proxy configuration would weaken its read-only boundary."""


@dataclass(frozen=True, slots=True)
class ProxySettings:
    listen_host: str
    listen_port: int
    client_user: str
    client_password: str
    upstream_url: str
    upstream_user: str
    upstream_password: str
    database: str = "iot"
    timeout_seconds: int = 8
    max_rows: int = 2000
    max_window_hours: int = 168
    max_request_bytes: int = 8192
    max_response_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_env(cls) -> ProxySettings:
        return cls(
            listen_host=os.getenv("AIOPS_TD_PROXY_LISTEN_HOST", "127.0.0.1").strip(),
            listen_port=_env_int("AIOPS_TD_PROXY_LISTEN_PORT", 16041),
            client_user=_required_env("AIOPS_TD_PROXY_CLIENT_USER"),
            client_password=_required_env("AIOPS_TD_PROXY_CLIENT_PASSWORD"),
            upstream_url=os.getenv("AIOPS_TD_PROXY_UPSTREAM_URL", "http://127.0.0.1:6041").strip(),
            upstream_user=_required_env("AIOPS_TD_PROXY_UPSTREAM_USER"),
            upstream_password=_required_env("AIOPS_TD_PROXY_UPSTREAM_PASSWORD"),
            database=os.getenv("AIOPS_TD_PROXY_DATABASE", "iot").strip(),
            timeout_seconds=_env_int("AIOPS_TD_PROXY_TIMEOUT_SECONDS", 8),
            max_rows=_env_int("AIOPS_TD_PROXY_MAX_ROWS", 2000),
            max_window_hours=_env_int("AIOPS_TD_PROXY_MAX_WINDOW_HOURS", 168),
            max_request_bytes=_env_int("AIOPS_TD_PROXY_MAX_REQUEST_BYTES", 8192),
            max_response_bytes=_env_int("AIOPS_TD_PROXY_MAX_RESPONSE_BYTES", 16 * 1024 * 1024),
        )

    def validate(self) -> None:
        try:
            listen_address = ipaddress.ip_address(self.listen_host)
        except ValueError as exc:
            raise ProxyConfigError("proxy listen host must be an IP address") from exc
        if not listen_address.is_loopback:
            raise ProxyConfigError("proxy must listen on a loopback address")

        upstream = urlsplit(self.upstream_url)
        if upstream.scheme != "http" or not upstream.hostname or upstream.path not in {"", "/"}:
            raise ProxyConfigError("upstream URL must be a plain HTTP origin")
        try:
            upstream_address = ipaddress.ip_address(upstream.hostname)
            upstream_port = upstream.port
        except ValueError as exc:
            raise ProxyConfigError("upstream host and port must be valid") from exc
        if not upstream_address.is_loopback:
            raise ProxyConfigError("upstream must be on a loopback address")
        if upstream_port is not None and not 1 <= upstream_port <= 65535:
            raise ProxyConfigError("upstream port must be valid")
        if upstream.query or upstream.fragment or upstream.username or upstream.password:
            raise ProxyConfigError("upstream URL must not contain credentials, query, or fragment")
        if not SAFE_DATABASE.fullmatch(self.database) or self.database != "iot":
            raise ProxyConfigError("proxy is restricted to the iot database")
        if not 1 <= self.listen_port <= 65535:
            raise ProxyConfigError("invalid listen port")
        if not 1 <= self.timeout_seconds <= 60:
            raise ProxyConfigError("invalid upstream timeout")
        if not 1 <= self.max_rows <= 2000:
            raise ProxyConfigError("invalid row limit")
        if not 1 <= self.max_window_hours <= 168:
            raise ProxyConfigError("invalid time-window limit")
        if not 1024 <= self.max_request_bytes <= 65_536:
            raise ProxyConfigError("invalid request-size limit")
        if not 1024 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ProxyConfigError("invalid response-size limit")
        for value in (
            self.client_user,
            self.client_password,
            self.upstream_user,
            self.upstream_password,
        ):
            if not value or "\n" in value or "\r" in value:
                raise ProxyConfigError("credentials must be non-empty single-line values")


def is_allowed_sql(sql: str, settings: ProxySettings) -> bool:
    if len(sql.encode("utf-8")) > settings.max_request_bytes:
        return False
    if any(marker in sql for marker in (";", "--", "/*", "*/", "#", "\x00")):
        return False
    normalized = " ".join(sql.strip().split())
    if normalized == "SHOW STABLES":
        return True
    match = GUN_QUERY.fullmatch(normalized) or COMM_QUERY.fullmatch(normalized)
    if not match or int(match.group("limit")) > settings.max_rows:
        return False
    try:
        start = datetime.strptime(match.group("start"), "%Y-%m-%d %H:%M:%S.%f")
        end = datetime.strptime(match.group("end"), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return False
    return start <= end and end - start <= timedelta(hours=settings.max_window_hours)


class TDengineReadonlyHandler(BaseHTTPRequestHandler):
    settings: ClassVar[ProxySettings]
    server_version = "AI-Ops-TDengine-Readonly/1.0"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.settings.timeout_seconds)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        settings = self.settings
        expected_path = f"/rest/sql/{settings.database}"
        if self.path.rstrip("/") != expected_path:
            self._send_json(404, {"error": "not_found"})
            return
        if not _authorized(self.headers.get("Authorization"), settings):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="aiops-tdengine-readonly"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "chunked_requests_are_not_supported"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(411, {"error": "content_length_required"})
            return
        if not 1 <= content_length <= settings.max_request_bytes:
            self._send_json(413, {"error": "request_too_large"})
            return
        try:
            sql = self.rfile.read(content_length).decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {"error": "sql_must_be_utf8"})
            return
        if not is_allowed_sql(sql, settings):
            self._send_json(403, {"error": "query_not_allowed"})
            return
        self._forward(sql)

    def _forward(self, sql: str) -> None:
        settings = self.settings
        endpoint = f"{settings.upstream_url.rstrip('/')}/rest/sql/{settings.database}"
        request = urllib.request.Request(endpoint, data=sql.encode("utf-8"), method="POST")
        token = base64.b64encode(f"{settings.upstream_user}:{settings.upstream_password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Content-Type", "text/plain; charset=utf-8")
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=settings.timeout_seconds) as response:
                body = response.read(settings.max_response_bytes + 1)
                if len(body) > settings.max_response_bytes:
                    self._send_json(502, {"error": "upstream_response_too_large"})
                    return
                self._send_body(response.status, body, response.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            body = exc.read(settings.max_response_bytes + 1)
            if len(body) > settings.max_response_bytes:
                body = b'{"error":"upstream_response_too_large"}'
            self._send_body(exc.code, body, exc.headers.get_content_type())
        except (urllib.error.URLError, TimeoutError):
            self._send_json(502, {"error": "upstream_unavailable"})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        self._send_body(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def _send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(settings: ProxySettings) -> ThreadingHTTPServer:
    settings.validate()
    handler = type("ConfiguredTDengineReadonlyHandler", (TDengineReadonlyHandler,), {"settings": settings})
    server = ThreadingHTTPServer((settings.listen_host, settings.listen_port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    settings = ProxySettings.from_env()
    with make_server(settings) as server:
        server.serve_forever(poll_interval=0.5)


def _authorized(header: str | None, settings: ProxySettings) -> bool:
    if not header:
        return False
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded, validate=True)
        user, password = decoded.split(b":", 1)
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(user, settings.client_user.encode("utf-8")) and hmac.compare_digest(
        password, settings.client_password.encode("utf-8")
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ProxyConfigError(f"{name} is required")
    return value


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


if __name__ == "__main__":
    main()
