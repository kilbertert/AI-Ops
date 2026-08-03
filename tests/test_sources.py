from datetime import datetime

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.sources import (
    MySQLSource,
    RedisSource,
    TDengineSource,
    _safe_identifier,
    _safe_literal,
)


def test_tdengine_literal_rejects_injection_characters() -> None:
    with pytest.raises(ValueError):
        _safe_literal("device' OR 1=1 --")
    with pytest.raises(ValueError):
        _safe_literal("PILE--01")


def test_database_identifier_is_restricted() -> None:
    assert _safe_identifier("cloud_charging_pile") == "cloud_charging_pile"
    with pytest.raises(ValueError):
        _safe_identifier("cloud_charging_pile;DROP")


class _FakeCursor:
    def __init__(
        self,
        grants: str | list[str] = "GRANT SELECT ON cloud_charging_pile.* TO diagnostic",
        role_grants: str | list[str] | None = None,
        role_error: bool = False,
    ) -> None:
        self.calls = []
        self.grants = [grants] if isinstance(grants, str) else grants
        self.role_grants = [role_grants] if isinstance(role_grants, str) else role_grants or []
        self.role_error = role_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if " USING " in sql and self.role_error:
            raise RuntimeError("role expansion denied")

    def fetchall(self):
        if " USING " in self.calls[-1][0]:
            return [{"Grants": grant} for grant in self.role_grants]
        if self.calls[-1][0] == "SHOW GRANTS FOR CURRENT_USER()":
            return [{"Grants": grant} for grant in self.grants]
        return [{"order_no": "ORDER-123456", "tx_data": '{"txSerialNo":"TX-1"}'}]

    def fetchone(self):
        return {"version": "8.4", "current_user": "diagnostic@%", "time_zone": "+08:00"}


class _FakeConnection:
    def __init__(
        self,
        grants: str | list[str] = "GRANT SELECT ON cloud_charging_pile.* TO diagnostic",
        role_grants: str | list[str] | None = None,
        role_error: bool = False,
        rollback_error: bool = False,
    ) -> None:
        self.fake_cursor = _FakeCursor(grants, role_grants, role_error)
        self.rolled_back = False
        self.closed = False
        self.rollback_error = rollback_error

    def cursor(self):
        return self.fake_cursor

    def rollback(self):
        self.rolled_back = True
        if self.rollback_error:
            raise RuntimeError("connection lost")

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


def test_mysql_device_query_is_tenant_scoped(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    connection = _FakeConnection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    MySQLSource(settings).get_device(None, "PILE-01", "TENANT-1")

    sql, params = connection.fake_cursor.calls[2]
    assert "device_code=%s AND tenant_id=%s" in sql
    assert params == ["PILE-01", "TENANT-1"]


def test_mysql_connection_closes_when_rollback_fails(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    connection = _FakeConnection(rollback_error=True)
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    MySQLSource(settings).get_orders("ORDER-123456", "TENANT-1")

    assert connection.rolled_back is True
    assert connection.closed is True


def test_tdengine_query_is_bounded_by_device_time_and_limit() -> None:
    settings = Settings.from_env()
    settings.tdengine.user = "readonly"
    settings.tdengine.password = "secret"
    source = TDengineSource(settings)
    captured = []
    source._query = lambda sql: captured.append(sql) or []  # type: ignore[method-assign]

    source.get_gun_samples(
        "GUN-01",
        datetime.fromisoformat("2026-07-31 10:00:00"),
        datetime.fromisoformat("2026-07-31 10:30:00"),
        "TX-01",
    )

    sql = captured[0]
    assert "device='GUN-01'" in sql
    assert "`txSerialNo`='TX-01'" in sql
    assert "`batteryMaxTemperature`" in sql
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


def test_mysql_doctor_expands_assigned_roles(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "diagnostic"
    settings.mysql.password = "secret"
    connection = _FakeConnection(
        "GRANT `app_readwrite`@`%` TO `diagnostic`@`%`",
        "GRANT SELECT, UPDATE ON cloud_charging_pile.* TO `app_readwrite`@`%`",
    )
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    details = MySQLSource(settings).doctor()

    assert details["assigned_roles"] == ["`app_readwrite`@`%`"]
    assert details["read_only"] is False
    assert details["unsafe_privileges"] == ["UPDATE"]


def test_mysql_doctor_treats_unresolved_roles_as_unsafe(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "diagnostic"
    settings.mysql.password = "secret"
    connection = _FakeConnection(
        "GRANT `app_readonly`@`%` TO `diagnostic`@`%`",
        role_error=True,
    )
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    details = MySQLSource(settings).doctor()

    assert details["read_only"] is False
    assert details["unsafe_privileges"] == ["UNRESOLVED ROLE `app_readonly`@`%`"]


def test_mysql_doctor_treats_unparsed_role_assignments_as_unsafe(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.mysql.user = "diagnostic"
    settings.mysql.password = "secret"
    connection = _FakeConnection("GRANT `read only`@`%` TO `diagnostic`@`%`")
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)

    details = MySQLSource(settings).doctor()

    assert details["read_only"] is False
    assert details["unsafe_privileges"] == ["UNRESOLVED ROLE ASSIGNMENT"]


class _BinaryRedisClient:
    def type(self, stream):
        return b"stream" if stream == b"third.order.sync.queue" else b"none"

    def xinfo_groups(self, stream):
        return [{b"name": b"mall", b"consumers": 1, b"pending": 4, b"lag": 2}]

    def xrevrange(self, stream, count):
        return [(b"1-0", {b"payload": b"\x80binary TEST-ORDER-1234\xff"})]

    def xlen(self, stream):
        return 1

    def close(self):
        return None


def test_redis_stream_inspection_handles_binary_values(monkeypatch) -> None:
    settings = Settings.from_env()
    settings.redis.password = "secret"
    source = RedisSource(settings)
    monkeypatch.setattr(source, "_client", lambda: _BinaryRedisClient())

    result = source.inspect_streams("TEST-ORDER-1234")

    assert result[0]["matches"] == 1
    assert result[0]["groups"][0]["name"] == "mall"
    assert result[1]["type"] == "none"
