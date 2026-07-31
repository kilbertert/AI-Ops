from __future__ import annotations

import base64
import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.sources import TDengineSource
from aiops_diagnostics.tdengine_proxy import ProxyConfigError, ProxySettings, is_allowed_sql, make_server


def _settings(**overrides) -> ProxySettings:
    values = {
        "listen_host": "127.0.0.1",
        "listen_port": _available_port(),
        "client_user": "diagnostic_readonly",
        "client_password": "client-secret",
        "upstream_url": "http://127.0.0.1:6041",
        "upstream_user": "proxy-backend",
        "upstream_password": "upstream-secret",
    }
    values.update(overrides)
    return ProxySettings(**values)


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_runtime_queries_match_proxy_allowlist() -> None:
    settings = Settings.from_env()
    source = TDengineSource(settings)
    queries = []
    source._query = lambda sql: queries.append(sql) or []  # type: ignore[method-assign]

    source.get_gun_samples(
        "GUN-01",
        datetime.fromisoformat("2026-07-31 10:00:00"),
        datetime.fromisoformat("2026-07-31 10:30:00"),
        "TX-01",
    )
    source.get_comm_messages(
        "GUN-01",
        datetime.fromisoformat("2026-07-31 10:00:00"),
        datetime.fromisoformat("2026-07-31 10:30:00"),
    )

    proxy = _settings()
    assert is_allowed_sql("SHOW STABLES", proxy)
    assert all(is_allowed_sql(query, proxy) for query in queries)


def test_gun_query_quotes_case_sensitive_tdengine_columns() -> None:
    settings = Settings.from_env()
    source = TDengineSource(settings)
    queries = []
    source._query = lambda sql: queries.append(sql) or []  # type: ignore[method-assign]

    source.get_gun_samples(
        "GUN-01",
        datetime.fromisoformat("2026-07-31 10:00:00"),
        datetime.fromisoformat("2026-07-31 10:30:00"),
        "TX-01",
    )

    assert "`txSerialNo`" in queries[0]
    assert "`errorReason`" in queries[0]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO charging-gun_property VALUES (NOW, 1)",
        "DROP DATABASE iot",
        "SELECT * FROM `charging-gun_property` LIMIT 1",
        "SHOW STABLES; DROP DATABASE iot",
        "SHOW DATABASES",
    ],
)
def test_proxy_rejects_non_allowlisted_sql(sql: str) -> None:
    assert not is_allowed_sql(sql, _settings())


def test_proxy_rejects_large_or_reversed_windows() -> None:
    sql = (
        "SELECT _ts, direction, code, decoded FROM `charging-pile_comm` "
        "WHERE device='GUN-01' AND _ts>='2026-07-31 11:00:00.000' "
        "AND _ts<='2026-07-31 10:00:00.000' ORDER BY _ts ASC LIMIT 2000"
    )
    assert not is_allowed_sql(sql, _settings())

    oversized = sql.replace("LIMIT 2000", "LIMIT 2001").replace(
        "2026-07-31 11:00:00.000", "2026-07-31 09:00:00.000"
    )
    assert not is_allowed_sql(oversized, _settings())


def test_proxy_requires_loopback_endpoints() -> None:
    with pytest.raises(ProxyConfigError, match="loopback"):
        _settings(listen_host="0.0.0.0").validate()
    with pytest.raises(ProxyConfigError, match="loopback"):
        _settings(upstream_url="http://192.168.0.40:6041").validate()


def test_http_proxy_forwards_allowed_query_and_rejects_mutation() -> None:
    captured = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            captured.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
            body = json.dumps({"code": 0, "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = make_server(
        _settings(
            upstream_url=f"http://127.0.0.1:{upstream.server_port}",
        )
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    token = base64.b64encode(b"diagnostic_readonly:client-secret").decode()

    try:
        allowed = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/rest/sql/iot",
            data=b"SHOW STABLES",
            headers={"Authorization": f"Basic {token}"},
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            assert json.loads(response.read())["code"] == 0

        unauthorized = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/rest/sql/iot",
            data=b"SHOW STABLES",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(unauthorized, timeout=2)
        assert exc_info.value.code == 401

        denied = urllib.request.Request(
            f"http://127.0.0.1:{proxy.server_port}/rest/sql/iot",
            data=b"DROP DATABASE iot",
            headers={"Authorization": f"Basic {token}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(denied, timeout=2)
        assert exc_info.value.code == 403
        assert captured == ["SHOW STABLES"]
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
