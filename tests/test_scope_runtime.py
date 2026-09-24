from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.query_scope import QueryScope, resolve_query_scope, static_site_mapper
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ORGAN,
    DataScope,
    ScopeRequest,
    ScopeResolver,
    SubjectRecord,
    UserContext,
)
from aiops_diagnostics.sources import (
    DeviceGate,
    FixtureSources,
    MySQLSource,
    RedisSource,
    ScopedSources,
    SourceError,
    TDengineSource,
    live_sources,
    scoped_live_sources,
)

TENANT = "TENANT-A"
SITES = ("SITE-A-1",)


def _caller() -> UserContext:
    return UserContext(
        subject=SubjectRecord(
            b_user_id="B-CALLER-1",
            c_user_id="C-CALLER-1",
            username="ops.caller",
            tenant_id=TENANT,
        )
    )


class _FakeDirectory:
    def __init__(self, caller: UserContext, business_scope: DataScope) -> None:
        self.caller = caller
        self.business_scope = business_scope

    def user_info(self, credential: str) -> UserContext:
        return self.caller

    def user_by_b_user_id(self, credential: str, b_user_id: str) -> SubjectRecord | None:
        return None

    def users_by_c_user_id(self, credential: str, c_user_id: str) -> tuple[SubjectRecord, ...]:
        return ()

    def data_scope(self, credential: str) -> DataScope:
        return self.business_scope


def _query_scope() -> QueryScope:
    directory = _FakeDirectory(
        _caller(),
        DataScope(type=SCOPE_TYPE_ORGAN, site_ids=SITES, shop_ids=(), organ_ids=()),
    )
    resolver = ScopeResolver(directory)
    context = resolver.resolve(ScopeRequest(credential="platform-token-ops-1"))
    return resolve_query_scope(context, mapper=static_site_mapper())


def test_device_gate_collects_order_devices() -> None:
    gate = DeviceGate()

    gate.seed_from_orders(
        [
            {"device_code": "PILE-01", "child_device_code": "GUN-01"},
            {"device_code": "PILE-01"},
            {},
        ]
    )

    assert gate.allowed_devices() == frozenset({"PILE-01", "GUN-01"})


def test_device_gate_empty_until_seeded() -> None:
    gate = DeviceGate()

    assert gate.allowed_devices() == frozenset()


def test_scoped_sources_pushes_scope_to_mysql_tdengine_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _query_scope()
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"

    class _FakeConn:
        def __init__(self) -> None:
            self.executed: list[tuple[str, list[Any]]] = []

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.executed.append((sql, list(params or [])))

        def fetchall(self):
            return [{"id": "PILE-01", "device_code": "PILE-01"}]

        def fetchone(self):
            return {"id": "PILE-01", "device_code": "PILE-01", "tenant_id": TENANT}

        def rollback(self):
            return None

        def close(self):
            return None

    conn = _FakeConn()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: conn)

    class _FakeRedis:
        def __init__(self) -> None:
            self.calls = []

        def type(self, stream):
            return b"none"

        def close(self):
            return None

    gate = DeviceGate()
    sources = ScopedSources(
        settings,
        mysql=MySQLSource(settings, scope=scope),
        tdengine=TDengineSource(settings, allowed_devices=frozenset()),
        redis=RedisSource(settings, order_in_scope=lambda f: True),
        device_gate=gate,
    )

    sources.get_orders("O-1")

    # MySQL 查询（真实 SELECT）必须带 tenant 过滤
    selects = [sql for sql, _ in conn.executed if "SELECT" in sql]
    assert selects and all("tenant_id=%s" in sql for sql in selects)

    # TDengine 在 seed 后允许订单设备
    assert gate.allowed_devices() == frozenset({"PILE-01"})


def test_scoped_sources_tdengine_rejects_unknown_device() -> None:
    scope = _query_scope()
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.tdengine.user = "readonly"
    settings.tdengine.password = "secret"

    gate = DeviceGate()
    gate.seed_from_orders([{"device_code": "PILE-01"}])
    sources = ScopedSources(
        settings,
        mysql=MySQLSource(settings, scope=scope),
        tdengine=TDengineSource(settings, allowed_devices=gate.allowed_devices()),
        redis=RedisSource(settings),
        device_gate=gate,
    )

    with pytest.raises(SourceError, match="设备不在当前权限范围"):
        sources.get_comm_messages(
            "PILE-EVIL",
            datetime.fromisoformat("2026-07-31 10:00:00"),
            datetime.fromisoformat("2026-07-31 10:30:00"),
        )


def test_live_sources_defaults_unscoped_still_work() -> None:
    # 不提供 scope 时 live_sources 行为不变（走 HybridSources）
    settings = Settings.from_env()
    settings.ssh.enabled = False
    with live_sources(settings) as sources:
        assert hasattr(sources, "get_orders")


def test_scoped_live_sources_requires_valid_query_scope() -> None:
    with pytest.raises(ValueError, match="缺少有效租户"):
        QueryScope(tenant_id="", site_ids=None, user_id=None)


def test_fixture_sources_still_work_with_scope_code_path() -> None:
    fixture = Path("examples/fixtures/ocpp_consistent.json")
    sources = FixtureSources(fixture)

    assert sources.get_orders("ORDER-1") == sources.get_orders("ORDER-1")


def test_scoped_live_sources_constructs_scoped_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env()
    settings.ssh.enabled = False
    scope = _query_scope()

    monkeypatch.setattr(
        "aiops_diagnostics.sources._ssh_tunnel",
        lambda s, forwards=("mysql", "tdengine", "redis"): contextlib.nullcontext(s),
    )

    with scoped_live_sources(settings, scope=scope) as sources:
        assert isinstance(sources.mysql, MySQLSource)
        assert isinstance(sources.tdengine, TDengineSource)
        assert isinstance(sources.redis, RedisSource)
        # TDengine 初始 fail closed（空设备集合）
        assert sources.tdengine.allowed_devices == frozenset()
