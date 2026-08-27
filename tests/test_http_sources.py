from __future__ import annotations

import hashlib
import hmac
import io
import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.sources import FixtureSources, HttpSources, SourceError

FIXTURES = Path(__file__).parents[1] / "examples" / "fixtures"


def _http_settings() -> Settings:
    settings = Settings.from_env()
    settings.diag_api.base_url = "https://diag.example.test"
    settings.diag_api.token_secret = "test-secret"
    settings.diag_api.token_expire_seconds = 300
    settings.diag_api.timeout_seconds = 5
    return settings


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _RawResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _RawResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


_EMPTY_FIXTURE: dict[str, Any] = {
    "orders": [],
    "fee_template_records": {},
    "devices": [],
    "gun_samples": [],
    "comm_messages": [],
    "streams": [],
}


class _FakeDiagTransport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[tuple[str, list[tuple[str, str]]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> _FakeResponse:
        self.requests.append((request.full_url, list(request.header_items())))
        split = urlsplit(request.full_url)
        params = {key: values[-1] for key, values in parse_qs(split.query).items()}
        return _FakeResponse({"code": 0, "msg": "ok", "data": self._data_for(split.path, params)})

    def _data_for(self, path: str, params: dict[str, str]) -> Any:
        if path == "/diag/order":
            order_no = params["order_no"]
            tenant_id = params.get("tenant_id")
            orders = [
                row
                for row in self.payload.get("orders", [])
                if row.get("order_no") == order_no and (not tenant_id or row.get("tenant_id") == tenant_id)
            ]
            fee_template = None
            if params.get("include_fee_template") == "true":
                record = self.payload.get("fee_template_records", {}).get(order_no)
                if record and (not tenant_id or record.get("tenant_id") == tenant_id):
                    fee_template = record
            return {"orders": orders, "fee_template": fee_template}

        if path == "/diag/device":
            device_id = params.get("device_id")
            device_code = params.get("device_code")
            tenant_id = params.get("tenant_id")
            for device in self.payload.get("devices", []):
                if (
                    (device_id and device.get("id") == device_id)
                    or (device_code and device.get("device_code") == device_code)
                ) and (not tenant_id or not device.get("tenant_id") or device.get("tenant_id") == tenant_id):
                    return device
            return None

        if path == "/diag/gun-property":
            return self.payload.get("gun_samples", [])
        if path == "/diag/comm-message":
            return self.payload.get("comm_messages", [])
        if path == "/diag/redis-stream":
            return self.payload.get("streams", [])
        raise AssertionError(f"unexpected diag endpoint: {path}")


def test_http_sources_adds_internal_token_headers(monkeypatch) -> None:
    transport = _FakeDiagTransport(_EMPTY_FIXTURE)
    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", transport)
    monkeypatch.setattr("aiops_diagnostics.sources.time.time", lambda: 1_760_000_000)
    source = HttpSources(_http_settings())

    source.get_orders("TEST-YKC-0001", "TENANT-DEMO")

    _, headers = transport.requests[0]
    headers_by_name = {name: value for name, value in headers}
    timestamp = "1760000000"
    message = f"{timestamp}:300"
    expected_token = hmac.new(
        b"test-secret",
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers_by_name["X-request-timestamp"] == timestamp
    assert headers_by_name["X-internal-token"] == expected_token
    assert headers_by_name["Accept"] == "application/json"


def test_http_sources_uses_frozen_diag_endpoint_contract(monkeypatch) -> None:
    transport = _FakeDiagTransport(_EMPTY_FIXTURE)
    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", transport)
    source = HttpSources(_http_settings())
    start = datetime.fromisoformat("2026-07-31 10:00:00")
    end = datetime.fromisoformat("2026-07-31 10:30:00")

    source.get_orders("TEST-YKC-0001", "TENANT-DEMO")
    source.get_fee_template_record("TEST-YKC-0001", "TENANT-DEMO")
    source.get_device("DEVICE-ID-1", None, "TENANT-DEMO")
    source.get_gun_samples("GUN-DEMO-01", start, end, "TX-DEMO-0001")
    source.get_comm_messages("PILE-DEMO-01", start, end)
    source.inspect_streams("TEST-YKC-0001")

    requests = []
    for url, _ in transport.requests:
        split = urlsplit(url)
        requests.append((split.path, {key: values[-1] for key, values in parse_qs(split.query).items()}))

    assert requests[0] == (
        "/diag/order",
        {
            "order_no": "TEST-YKC-0001",
            "tenant_id": "TENANT-DEMO",
            "include_fee_template": "false",
        },
    )
    assert requests[1] == (
        "/diag/order",
        {
            "order_no": "TEST-YKC-0001",
            "tenant_id": "TENANT-DEMO",
            "include_fee_template": "true",
        },
    )
    assert requests[2] == (
        "/diag/device",
        {"device_id": "DEVICE-ID-1", "tenant_id": "TENANT-DEMO"},
    )
    assert requests[3] == (
        "/diag/gun-property",
        {
            "device": "GUN-DEMO-01",
            "start_time": "2026-07-31T10:00:00.000",
            "end_time": "2026-07-31T10:30:00.000",
            "tx_serial_no": "TX-DEMO-0001",
        },
    )
    assert requests[4] == (
        "/diag/comm-message",
        {
            "device": "PILE-DEMO-01",
            "start_time": "2026-07-31T10:00:00.000",
            "end_time": "2026-07-31T10:30:00.000",
        },
    )
    assert requests[5] == (
        "/diag/redis-stream",
        {"order_no": "TEST-YKC-0001"},
    )


def test_http_sources_rejects_failed_or_expired_token(monkeypatch) -> None:
    for status in (401, 403):

        def raise_http_error(request: Any, timeout: int | None = None, status: int = status) -> None:
            body = json.dumps({"code": status, "msg": "令牌无效或过期"}).encode("utf-8")
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "Unauthorized",
                {},
                io.BytesIO(body),
            )

        monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", raise_http_error)
        source = HttpSources(_http_settings())

        with pytest.raises(SourceError, match="令牌无效或过期"):
            source.get_orders("TEST-YKC-0001")


def test_http_sources_rejects_api_failure_codes(monkeypatch) -> None:
    def fail_transport(request: Any, timeout: int | None = None) -> _FakeResponse:
        return _FakeResponse({"code": 500, "msg": "订单查询失败", "data": None})

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", fail_transport)
    source = HttpSources(_http_settings())

    with pytest.raises(SourceError, match="订单查询失败"):
        source.get_orders("TEST-YKC-0001")


def test_http_sources_wraps_non_utf8_success_body(monkeypatch) -> None:
    def invalid_utf8_transport(request: Any, timeout: int | None = None) -> _RawResponse:
        return _RawResponse(b"\xff\xfe")

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", invalid_utf8_transport)
    source = HttpSources(_http_settings())

    with pytest.raises(SourceError, match="UnicodeDecodeError"):
        source.get_orders("TEST-YKC-0001")


def test_http_sources_requires_diag_api_config_before_request(monkeypatch) -> None:
    requested: list[str] = []

    def forbidden_transport(request: Any, timeout: int | None = None) -> _RawResponse:
        requested.append(request.full_url)
        raise AssertionError("Diag API must not be called before configuration is validated")

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", forbidden_transport)
    for field in ("base_url", "token_secret"):
        settings = _http_settings()
        setattr(settings.diag_api, field, "")

        with pytest.raises(SourceError):
            HttpSources(settings).get_orders("TEST-YKC-0001")

    assert requested == []


def test_diag_api_base_url_rejects_unsafe_components(monkeypatch) -> None:
    for base_url in (
        "ftp://127.0.0.1:8080",
        "https://user:pass@diag.example.test",
        "https://diag.example.test?from=proxy",
        "https://diag.example.test#debug",
    ):
        monkeypatch.setenv("AIOPS_DIAG_API_BASE_URL", base_url)
        with pytest.raises(ValueError, match="Diag API base_url"):
            Settings.from_env()


def test_diag_api_settings_enforce_numeric_bounds(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DIAG_API_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="超时"):
        Settings.from_env()

    monkeypatch.setenv("AIOPS_DIAG_API_TIMEOUT_SECONDS", "61")
    with pytest.raises(ValueError, match="超时"):
        Settings.from_env()

    monkeypatch.setenv("AIOPS_DIAG_API_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_EXPIRE_SECONDS", "59")
    with pytest.raises(ValueError, match="令牌有效期"):
        Settings.from_env()

    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_EXPIRE_SECONDS", "601")
    with pytest.raises(ValueError, match="令牌有效期"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("get_orders", {"order_no": "ORDER' OR 1=1 --"}),
        (
            "get_device",
            {"device_id": None, "device_code": "PILE--01", "tenant_id": None},
        ),
        (
            "get_gun_samples",
            {
                "device": "GUN' OR 1=1 --",
                "start_time": datetime.fromisoformat("2026-07-31 10:00:00"),
                "end_time": datetime.fromisoformat("2026-07-31 10:30:00"),
                "tx_serial_no": None,
            },
        ),
        (
            "get_comm_messages",
            {
                "device": "PILE--01",
                "start_time": datetime.fromisoformat("2026-07-31 10:00:00"),
                "end_time": datetime.fromisoformat("2026-07-31 10:30:00"),
            },
        ),
        ("inspect_streams", {"order_no": "ORDER 1"}),
    ],
)
def test_http_sources_preserve_identifier_injection_guard(
    monkeypatch, method: str, kwargs: dict[str, Any]
) -> None:
    transport = _FakeDiagTransport(_EMPTY_FIXTURE)
    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", transport)
    source = HttpSources(_http_settings())

    with pytest.raises(ValueError):
        getattr(source, method)(**kwargs)
    assert transport.requests == []


@pytest.mark.parametrize(
    "fixture_name",
    [
        "ykc_amount_mismatch.json",
        "ocpp_consistent.json",
        "missing_tx_data.json",
    ],
)
def test_http_sources_match_fixture_sources_field_by_field(monkeypatch, fixture_name: str) -> None:
    fixture = FixtureSources(FIXTURES / fixture_name)
    transport = _FakeDiagTransport(fixture.payload)
    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", transport)
    source = HttpSources(_http_settings())

    for order in fixture.payload["orders"]:
        order_no = order["order_no"]
        tenant_id = order.get("tenant_id")
        device = order.get("child_device_code") or order.get("device_code")
        start, end = _fixture_window(order)

        assert source.get_orders(order_no, tenant_id) == fixture.get_orders(order_no, tenant_id)
        assert source.get_fee_template_record(order_no, tenant_id) == fixture.get_fee_template_record(
            order_no, tenant_id
        )
        assert source.get_device(
            order.get("device_id"), order.get("device_code"), tenant_id
        ) == fixture.get_device(order.get("device_id"), order.get("device_code"), tenant_id)
        assert source.get_gun_samples(
            device, start, end, _fixture_tx_serial(order)
        ) == fixture.get_gun_samples(device, start, end, _fixture_tx_serial(order))
        assert source.get_comm_messages(
            order.get("device_code") or order.get("child_device_code"), start, end
        ) == fixture.get_comm_messages(order.get("device_code") or order.get("child_device_code"), start, end)
        assert source.inspect_streams(order_no) == fixture.inspect_streams(order_no)


def test_diag_api_secret_is_redacted(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DIAG_API_TOKEN_SECRET", "diag-secret")
    redacted = Settings.from_env().redacted()

    assert redacted["diag_api"]["token_secret"] == "REDACTED"
    assert "diag-secret" not in str(redacted)


def _fixture_window(order: dict[str, Any]) -> tuple[datetime, datetime]:
    created = datetime.fromisoformat(order["created_time"])
    stopped = datetime.fromisoformat(order["stop_time"]) if order.get("stop_time") else created
    return created - timedelta(minutes=5), stopped + timedelta(minutes=5)


def _fixture_tx_serial(order: dict[str, Any]) -> str | None:
    protocol = str(order.get("device_protocol") or "").upper()
    if protocol.startswith("OCPP"):
        return order.get("transaction_id") or (order.get("tx_data") or {}).get("txSerialNo")
    return order.get("order_no")
