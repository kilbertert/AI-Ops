from __future__ import annotations

import copy
import io
import json
import urllib.error
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from test_http_sources import (
    FIXTURES,
    _FakeDiagTransport,
    _fixture_tx_serial,
    _fixture_window,
    _http_settings,
)

from aiops_diagnostics.sources import (
    FixtureSources,
    HybridSources,
    LiveSources,
    SourceError,
    _safe_http_param,
    direct_sources,
    live_sources,
)


@pytest.mark.parametrize(
    "fixture_name",
    ["ykc_amount_mismatch.json", "ocpp_consistent.json", "missing_tx_data.json"],
)
def test_hybrid_sources_match_fixture_sources_field_by_field(monkeypatch, fixture_name: str) -> None:
    fixture = FixtureSources(FIXTURES / fixture_name)
    transport = _FakeDiagTransport(fixture.payload)
    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", transport)
    source = HybridSources(_http_settings())

    def tdengine_query(sql: str) -> list[dict[str, Any]]:
        key = "gun_samples" if "`charging-gun_property`" in sql else "comm_messages"
        return copy.deepcopy(fixture.payload.get(key, []))

    monkeypatch.setattr(source.tdengine, "_query", tdengine_query)

    for order in fixture.payload["orders"]:
        _assert_same_as_fixture(source, fixture, order)

    paths = [urlsplit(request).path for request, _headers in transport.requests]
    assert paths.count("/diag/order") == len(fixture.payload["orders"]) * 2
    assert paths.count("/diag/device") == len(fixture.payload["orders"])
    assert paths.count("/diag/redis-stream") == len(fixture.payload["orders"])
    assert "/diag/gun-property" not in paths
    assert "/diag/comm-message" not in paths


def _assert_same_as_fixture(source: HybridSources, fixture: FixtureSources, order: dict[str, Any]) -> None:
    order_no = order["order_no"]
    tenant_id = order.get("tenant_id")
    device = order.get("child_device_code") or order.get("device_code")
    device_key = order.get("device_code") or order.get("child_device_code")
    start, end = _fixture_window(order)
    tx_serial_no = _fixture_tx_serial(order)

    for actual, expected in (
        (source.get_orders(order_no, tenant_id), fixture.get_orders(order_no, tenant_id)),
        (
            source.get_fee_template_record(order_no, tenant_id),
            fixture.get_fee_template_record(order_no, tenant_id),
        ),
        (
            source.get_device(order.get("device_id"), order.get("device_code"), tenant_id),
            fixture.get_device(order.get("device_id"), order.get("device_code"), tenant_id),
        ),
        (
            source.get_gun_samples(device, start, end, tx_serial_no),
            fixture.get_gun_samples(device, start, end, tx_serial_no),
        ),
        (source.get_comm_messages(device_key, start, end), fixture.get_comm_messages(device_key, start, end)),
        (source.inspect_streams(order_no), fixture.inspect_streams(order_no)),
    ):
        assert actual == expected


def test_hybrid_sources_tdengine_sql_matches_live_sources(monkeypatch) -> None:
    source = HybridSources(_http_settings())
    captured: list[str] = []
    monkeypatch.setattr(source.tdengine, "_query", lambda sql: captured.append(sql) or [])
    start = datetime.fromisoformat("2026-07-31 10:00:00")
    end = datetime.fromisoformat("2026-07-31 10:30:00")

    source.get_gun_samples("GUN-01", start, end, "TX-01")
    source.get_comm_messages("PILE-01", start, end)

    assert all(
        item in captured[0]
        for item in (
            "device='GUN-01'",
            "`txSerialNo`='TX-01'",
            "FROM `charging-gun_property`",
            "_ts>='2026-07-31 10:00:00.000'",
            "LIMIT 2000",
        )
    )
    assert all(
        item in captured[1]
        for item in (
            "device='PILE-01'",
            "FROM `charging-pile_comm`",
            "_ts<='2026-07-31 10:30:00.000'",
            "LIMIT 2000",
        )
    )


def test_hybrid_sources_doctor_classification(monkeypatch) -> None:
    source = HybridSources(_http_settings())
    monkeypatch.setattr(
        "aiops_diagnostics.sources.urllib.request.urlopen",
        _FakeDiagTransport({"streams": []}),
    )
    monkeypatch.setattr(
        source.tdengine,
        "_query",
        lambda _sql: [
            {"stable_name": "charging-gun_property"},
            {"stable_name": "charging-pile_comm"},
        ],
    )

    success = source.doctor()
    assert list(success) == ["http", "tdengine", "mysql", "redis"]
    assert success["http"]["ok"] is True
    assert success["http"]["status"] == "ok"
    assert success["http"]["details"]["token_configured"] is True
    assert success["tdengine"]["ok"] is True
    assert success["tdengine"]["details"]["charging_gun_property"] is True
    assert success["tdengine"]["details"]["cutover"] == "partial"
    assert success["mysql"] == {
        "ok": True,
        "status": "deprecated",
        "details": {"message": "改由 /diag/* HTTP 接口访问；本仓 HybridSources 仍能 fallback，但生产应禁用"},
    }
    assert success["redis"] == success["mysql"]

    def fail(_sql: str) -> list[dict[str, Any]]:
        raise SourceError("TDengine 查询失败: URLError")

    monkeypatch.setattr(source.tdengine, "_query", fail)
    failed = source.doctor()
    assert failed["http"]["ok"] is True
    assert failed["tdengine"] == {
        "ok": False,
        "status": "error",
        "error": "TDengine 查询失败: URLError",
    }
    assert failed["mysql"]["status"] == "deprecated"


def test_hybrid_sources_doctor_http_config_missing(monkeypatch) -> None:
    settings = _http_settings()
    settings.http.base_url = ""
    source = HybridSources(settings)
    monkeypatch.setattr(source.tdengine, "_query", lambda _sql: [])

    def forbidden(request: Any, timeout: int | None = None) -> None:
        raise AssertionError("HTTP must not be called when Diag API config is missing")

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", forbidden)

    result = source.doctor()

    assert result["http"]["ok"] is False
    assert result["http"]["status"] == "error"
    assert result["http"]["error"] == "http.config_missing"


def test_hybrid_sources_doctor_http_auth_failed(monkeypatch) -> None:
    source = HybridSources(_http_settings())
    monkeypatch.setattr(source.tdengine, "_query", lambda _sql: [])

    def auth_failed(request: Any, timeout: int | None = None) -> _FakeDiagTransport:
        body = json.dumps({"code": 401, "msg": "令牌无效或过期"}).encode("utf-8")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", auth_failed)

    result = source.doctor()

    assert result["http"]["ok"] is False
    assert result["http"]["error"] == "http.auth_failed"
    assert "test-secret" not in str(result)
    assert "diag.example.test" not in str(result)


def test_hybrid_sources_doctor_http_unreachable(monkeypatch) -> None:
    source = HybridSources(_http_settings())
    monkeypatch.setattr(source.tdengine, "_query", lambda _sql: [])

    def unreachable(request: Any, timeout: int | None = None) -> _FakeDiagTransport:
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", unreachable)

    result = source.doctor()

    assert result["http"]["ok"] is False
    assert result["http"]["error"] == "http.http_unreachable"
    assert "test-secret" not in str(result)
    assert "diag.example.test" not in str(result)


def test_safe_http_param_allows_expected_values() -> None:
    assert _safe_http_param("2026-07-31T10:00:00.000") == "2026-07-31T10:00:00.000"


def test_safe_http_param_enforces_length_boundary() -> None:
    assert _safe_http_param("A" * 128) == "A" * 128
    with pytest.raises(ValueError):
        _safe_http_param("A" * 129)


@pytest.mark.parametrize("value", ["ORDER\r\nInjected", "PILE--01", "GUN' OR 1=1 --", "ORDER 1"])
def test_safe_http_param_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        _safe_http_param(value)


def test_live_sources_defaults_to_hybrid_sources_and_direct_sources_remains() -> None:
    settings = _http_settings()

    with live_sources(settings) as sources:
        assert isinstance(sources, HybridSources)

    with direct_sources(settings) as sources:
        assert isinstance(sources, LiveSources)
