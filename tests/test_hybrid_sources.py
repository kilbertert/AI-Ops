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
    _RawResponse,
)

from aiops_diagnostics.config import Settings
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

    def routing_query(sql: str) -> list[dict[str, Any]]:
        if sql.startswith("SHOW STABLES"):
            return [
                {"stable_name": "charging-gun_property"},
                {"stable_name": "charging-pile_comm"},
            ]
        if sql.startswith("DESCRIBE"):
            # 41 环境形态：缺 batteryMinTemperature，其余列齐全
            return [
                {"field": name}
                for name in (
                    "_ts",
                    "txSerialNo",
                    "status",
                    "isReturn",
                    "isInsert",
                    "outputVoltage",
                    "outputCurrent",
                    "power",
                    "chargingTime",
                    "chargingElectricityQuantity",
                    "soc",
                    "temperature",
                    "batteryMaxTemperature",
                    "errorCode",
                    "errorReason",
                    "meterNow",
                )
            ]
        return []

    monkeypatch.setattr(source.tdengine, "_query", routing_query)

    success = source.doctor()
    assert list(success) == ["http", "tdengine", "mysql", "redis"]
    assert success["http"]["ok"] is True
    assert success["http"]["status"] == "ok"
    assert success["http"]["details"]["token_configured"] is True
    assert success["tdengine"]["ok"] is True
    assert success["tdengine"]["details"]["charging_gun_property"] is True
    assert success["tdengine"]["details"]["cutover"] == "partial"
    assert success["tdengine"]["details"]["gun_columns"]["batteryMinTemperature"] is False
    assert success["tdengine"]["details"]["gun_columns"]["errorCode"] is True
    assert success["tdengine"]["details"]["gun_columns_checked"] is True
    assert success["mysql"] == {
        "ok": True,
        "status": "deprecated",
        "details": {"message": "已收口：证据改由 /diag/* HTTP 接口返回，生产配置不应保留直连凭据"},
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


@pytest.mark.parametrize(
    ("field", "missing_field"),
    [
        ("token_secret", "internal_token_secret"),
        ("token_expire_seconds", "internal_token_expire_seconds"),
    ],
)
def test_hybrid_sources_doctor_http_config_incomplete(monkeypatch, field: str, missing_field: str) -> None:
    settings = _http_settings()
    setattr(settings.diag_api, field, None)
    source = HybridSources(settings)
    monkeypatch.setattr(source.tdengine, "_query", lambda _sql: [])

    def forbidden(request: Any, timeout: int | None = None) -> None:
        raise AssertionError("HTTP must not be called when Diag API config is missing")

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", forbidden)

    result = source.doctor()

    assert result["http"]["ok"] is False
    assert result["http"]["status"] == "error"
    assert result["http"]["error"] == "http.config_missing"
    assert missing_field in result["http"]["details"]["missing"]


@pytest.mark.parametrize(
    ("payload_code", "expected_error"),
    [
        (401, "http.auth_failed"),
        (403, "http.auth_failed"),
        (500, "http.http_unreachable"),
    ],
)
def test_hybrid_sources_doctor_http_json_failure(monkeypatch, payload_code: int, expected_error: str) -> None:
    source = HybridSources(_http_settings())
    monkeypatch.setattr(source.tdengine, "_query", lambda _sql: [])

    def failing_api(request: Any, timeout: int | None = None) -> _RawResponse:
        body = json.dumps({"code": payload_code, "msg": "拒绝查询"}, ensure_ascii=False).encode("utf-8")
        return _RawResponse(body)

    monkeypatch.setattr("aiops_diagnostics.sources.urllib.request.urlopen", failing_api)

    result = source.doctor()

    assert result["http"]["ok"] is False
    assert result["http"]["status"] == "error"
    assert result["http"]["error"] == expected_error
    assert result["http"]["details"]["message"]
    assert "test-secret" not in str(result)
    assert "diag.example.test" not in str(result)


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


def _fake_ssh_runtime(monkeypatch, tmp_path) -> tuple[Settings, list[list[str]]]:
    settings = _http_settings()
    settings.ssh.enabled = True
    settings.ssh.ssh_bin = str(tmp_path / "ssh")
    settings.ssh.host = "bastion.example.test"
    settings.ssh.user = "diagnostic"
    settings.ssh.key_file = str(tmp_path / "id_ed25519")
    settings.ssh.mysql_host = "mysql.internal"
    settings.ssh.mysql_port = 3307
    settings.ssh.tdengine_host = "tdengine.internal"
    settings.ssh.tdengine_port = 16041
    settings.ssh.redis_host = "redis.internal"
    settings.ssh.redis_port = 6380
    (tmp_path / "ssh").write_text("")
    (tmp_path / "id_ed25519").write_text("")

    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str], **_: Any) -> None:
            commands.append(list(command))
            self.stderr = io.StringIO("")

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float = 3) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("SSH 隧道进程不应被强制结束")

    monkeypatch.setattr("aiops_diagnostics.sources.subprocess.Popen", FakePopen)
    monkeypatch.setattr("aiops_diagnostics.sources._wait_for_port", lambda *args, **kwargs: None)
    ports = iter([10101, 10102, 10103])
    monkeypatch.setattr("aiops_diagnostics.sources._available_port", lambda: next(ports))
    return settings, commands


def test_live_sources_ssh_tunnel_forwards_only_tdengine(monkeypatch, tmp_path) -> None:
    settings, commands = _fake_ssh_runtime(monkeypatch, tmp_path)

    with live_sources(settings) as sources:
        assert isinstance(sources, HybridSources)

    assert len(commands) == 1
    forwards = [commands[0][index + 1] for index, token in enumerate(commands[0]) if token == "-L"]
    assert forwards == ["127.0.0.1:10101:tdengine.internal:16041"]
    assert "mysql.internal" not in " ".join(commands[0])
    assert "redis.internal" not in " ".join(commands[0])


def test_direct_sources_ssh_tunnel_keeps_rollback_forwards(monkeypatch, tmp_path) -> None:
    settings, commands = _fake_ssh_runtime(monkeypatch, tmp_path)

    with direct_sources(settings) as sources:
        assert isinstance(sources, LiveSources)
        assert sources.tdengine.settings.tdengine.url == "http://127.0.0.1:10102"
        assert sources.mysql.settings.mysql.host == "127.0.0.1"
        assert sources.mysql.settings.mysql.port == 10101
        assert sources.redis.settings.redis.host == "127.0.0.1"
        assert sources.redis.settings.redis.port == 10103

    assert len(commands) == 1
    forwards = [commands[0][index + 1] for index, token in enumerate(commands[0]) if token == "-L"]
    assert forwards == [
        "127.0.0.1:10101:mysql.internal:3307",
        "127.0.0.1:10102:tdengine.internal:16041",
        "127.0.0.1:10103:redis.internal:6380",
    ]


def test_gun_select_derived_from_gun_columns_and_passes_proxy_guard(monkeypatch) -> None:
    """评审发现 #5 的 pin:gun SELECT 从 GUN_COLUMN_NAMES 派生,派生结果必须
    ① 与生产 TDengine 只读代理的正则白名单完全匹配(代理是独立部署服务,
    SQL 漂移会在滚动窗口内被拒);② 与既有线上 SQL 逐字节一致(加列或改
    引号风格都必须伴随代理同步部署,不允许静默改变 SQL 形状);③ 与
    doctor 逐列探测清单同源。"""
    from aiops_diagnostics.sources import GUN_COLUMNS_SQL, TDengineSource
    from aiops_diagnostics.tdengine_proxy import GUN_QUERY

    source = TDengineSource(_http_settings())
    start = datetime.fromisoformat("2026-07-31 10:00:00")
    end = datetime.fromisoformat("2026-07-31 10:30:00")

    captured: list[str] = []

    def fake_query(sql: str) -> list[dict[str, Any]]:
        captured.append(sql)
        return []

    monkeypatch.setattr(source, "_query", fake_query)
    source.get_gun_samples("GUN-01", start, end, "TX-01")

    sql = captured[0]
    match = GUN_QUERY.fullmatch(sql)
    assert match is not None, f"派生 SELECT 未通过只读代理白名单: {sql}"
    assert match.group("device") == "GUN-01"
    assert match.group("tx") == "TX-01"
    # 与线上 SQL 逐字节一致:只给含大写的列加反引号,全小写列裸写
    assert GUN_COLUMNS_SQL == (
        "_ts, `txSerialNo`, status, `isReturn`, `isInsert`, `outputVoltage`, `outputCurrent`, power, "
        "`chargingTime`, `chargingElectricityQuantity`, soc, temperature, `batteryMaxTemperature`, "
        "`batteryMinTemperature`, `errorCode`, `errorReason`, `meterNow`"
    )
