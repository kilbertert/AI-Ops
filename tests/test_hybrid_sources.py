from __future__ import annotations

import copy
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
        source.tdengine,
        "_query",
        lambda _sql: [
            {"stable_name": "charging-gun_property"},
            {"stable_name": "charging-pile_comm"},
        ],
    )

    success = source.doctor()
    assert success["diag_api"]["ok"] is True
    assert success["tdengine"]["ok"] is True
    assert success["tdengine"]["details"]["charging_gun_property"] is True

    def fail(_sql: str) -> list[dict[str, Any]]:
        raise SourceError("TDengine 查询失败: URLError")

    monkeypatch.setattr(source.tdengine, "_query", fail)
    failed = source.doctor()
    assert failed["diag_api"]["ok"] is True
    assert failed["tdengine"] == {"ok": False, "error": "TDengine 查询失败: URLError"}


def test_safe_http_param_allows_expected_values() -> None:
    assert _safe_http_param("2026-07-31T10:00:00.000") == "2026-07-31T10:00:00.000"


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
