from __future__ import annotations

from datetime import datetime

import pytest

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.data.sources import SourceError, TDengineSource

ALLOWED = frozenset({"GUN-01", "PILE-01"})
START = datetime.fromisoformat("2026-07-31 10:00:00")
END = datetime.fromisoformat("2026-07-31 10:30:00")


def _source(allowed_devices: frozenset[str] | None = None) -> TDengineSource:
    settings = Settings.from_env()
    settings.tdengine.user = "readonly"
    settings.tdengine.password = "secret"
    source = TDengineSource(settings, allowed_devices=allowed_devices)
    captured: list[str] = []
    source._query = lambda sql: captured.append(sql) or []  # type: ignore[method-assign]
    source.captured = captured  # type: ignore[attr-defined]
    return source


def test_tdengine_allows_device_within_scope() -> None:
    source = _source(ALLOWED)

    rows = source.get_gun_samples("GUN-01", START, END, None)

    assert rows == []
    assert source.captured  # type: ignore[attr-defined]
    assert "device='GUN-01'" in source.captured[0]  # type: ignore[attr-defined]


def test_tdengine_rejects_device_outside_scope_without_request() -> None:
    source = _source(ALLOWED)

    with pytest.raises(SourceError, match="设备不在当前权限范围") as excinfo:
        source.get_gun_samples("GUN-EVIL", START, END, None)

    assert excinfo.value.code == "tdengine.device_forbidden"
    assert not source.captured  # type: ignore[attr-defined]  # 不发 TDengine 请求


def test_tdengine_query_is_bounded_by_time_and_fixed_super_table() -> None:
    source = _source(ALLOWED)

    source.get_gun_samples("GUN-01", START, END, "TX-01")

    sql = source.captured[0]  # type: ignore[attr-defined]
    assert "FROM `charging-gun_property`" in sql
    assert "_ts>='2026-07-31 10:00:00.000'" in sql
    assert "_ts<='2026-07-31 10:30:00.000'" in sql
    assert "`txSerialNo`='TX-01'" in sql
    assert "LIMIT 2000" in sql


def test_tdengine_rejects_injection_in_device_identifier() -> None:
    source = _source(None)

    with pytest.raises(ValueError):
        source.get_gun_samples("GUN' OR 1=1 --", START, END, None)
