import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.sources import MySQLSource, TDengineSource, _safe_identifier, _safe_literal


def test_tdengine_literal_rejects_injection_characters() -> None:
    with pytest.raises(ValueError):
        _safe_literal("device' OR 1=1 --")


def test_database_identifier_is_restricted() -> None:
    assert _safe_identifier("cloud_charging_pile") == "cloud_charging_pile"
    with pytest.raises(ValueError):
        _safe_identifier("cloud_charging_pile;DROP")


class _FakeCursor:
    def __init__(self, grants: str = "GRANT SELECT ON cloud_charging_pile.* TO diagnostic") -> None:
        self.calls = []
        self.grants = grants

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        if self.calls[-1][0] == "SHOW GRANTS FOR CURRENT_USER()":
            return [{"Grants": self.grants}]
        return [{"order_no": "ORDER-123456", "tx_data": '{"txSerialNo":"TX-1"}'}]

    def fetchone(self):
        return {"version": "8.4", "current_user": "diagnostic@%", "time_zone": "+08:00"}


class _FakeConnection:
    def __init__(self, grants: str = "GRANT SELECT ON cloud_charging_pile.* TO diagnostic") -> None:
        self.fake_cursor = _FakeCursor(grants)
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_mysql_order_query_is_parameterized_and_read_only(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    connection = _FakeConnection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    rows = MySQLSource(settings).get_orders("ORDER-123456", "TENANT-1")

    statements = connection.fake_cursor.calls
    assert statements[1][0] == "START TRANSACTION READ ONLY"
    assert "order_no=%s AND tenant_id=%s" in statements[2][0]
    assert statements[2][1] == ["ORDER-123456", "TENANT-1"]
    assert rows[0]["tx_data"]["txSerialNo"] == "TX-1"
    assert connection.rolled_back and connection.closed


def test_tdengine_query_is_bounded_by_device_time_and_limit() -> None:
    settings = Settings.from_env()
    settings.tdengine.user = "readonly"
    settings.tdengine.password = "secret"
    source = TDengineSource(settings)
    captured = []
    source._query = lambda sql: captured.append(sql) or []  # type: ignore[method-assign]

    from datetime import datetime

    source.get_gun_samples(
        "GUN-01",
        datetime.fromisoformat("2026-07-31 10:00:00"),
        datetime.fromisoformat("2026-07-31 10:30:00"),
        "TX-01",
    )

    sql = captured[0]
    assert "device='GUN-01'" in sql
    assert "txSerialNo='TX-01'" in sql
    assert "_ts>='2026-07-31 10:00:00.000'" in sql
    assert "LIMIT 2000" in sql


def test_mysql_doctor_detects_write_privileges(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "diagnostic"
    settings.mysql.password = "secret"
    connection = _FakeConnection("GRANT SELECT, UPDATE ON *.* TO diagnostic")
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    details = MySQLSource(settings).doctor()

    assert "CURRENT_USER() AS `current_user`" in connection.fake_cursor.calls[2][0]
    assert details["read_only"] is False
    assert details["unsafe_privileges"] == ["UPDATE"]


def test_mysql_doctor_rejects_all_privileges(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "diagnostic"
    settings.mysql.password = "secret"
    connection = _FakeConnection("GRANT ALL PRIVILEGES ON *.* TO diagnostic WITH GRANT OPTION")
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    details = MySQLSource(settings).doctor()

    assert details["read_only"] is False
    assert details["unsafe_privileges"] == ["ALL PRIVILEGES", "GRANT OPTION"]
