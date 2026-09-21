from __future__ import annotations

from typing import Any

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.order_visibility import (
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    visible_orders,
)
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import (
    RedisSource,
    _order_in_scope,
    make_redis_order_in_scope,
)

TENANT = "TENANT-A"
ORDER = "ORDER-123456"


class _StreamClient:
    """记录 XREVRANGE 调用并返回可配置的 Stream 消息。"""

    def __init__(self, stream_payloads: dict[bytes, list[tuple[Any, dict[Any, Any]]]]) -> None:
        self.stream_payloads = stream_payloads
        self.calls: list[tuple[bytes, int]] = []

    def type(self, stream: bytes) -> bytes:
        return b"stream" if stream in self.stream_payloads else b"none"

    def xinfo_groups(self, stream: bytes) -> list[dict[Any, Any]]:
        return [{b"name": b"mall", b"consumers": 1, b"pending": 0, b"lag": 0}]

    def xrevrange(self, stream: bytes, count: int) -> list[tuple[Any, dict[Any, Any]]]:
        self.calls.append((stream, count))
        return self.stream_payloads.get(stream, [])

    def xlen(self, stream: bytes) -> int:
        return len(self.stream_payloads.get(stream, []))

    def close(self) -> None:
        return None


def _message(tenant: str | None = None, extra: dict[Any, Any] | None = None) -> tuple[bytes, dict[Any, Any]]:
    fields: dict[Any, Any] = {}
    if tenant is not None:
        fields[b"tenantId"] = tenant.encode("utf-8")
    fields[b"orderNo"] = ORDER.encode("utf-8")
    if extra:
        fields.update(extra)
    return b"1-0", fields


def _source(client: _StreamClient, scope: QueryScope | None = None) -> tuple[RedisSource, _StreamClient]:
    settings = Settings.from_env()
    settings.redis.password = "secret"
    source = RedisSource(
        settings,
        order_in_scope=make_redis_order_in_scope(scope) if scope else None,
    )
    source._client = lambda: client  # type: ignore[method-assign]
    return source, client


def _streams(tenant: str | None = None) -> dict[bytes, list[tuple[Any, dict[Any, Any]]]]:
    return {
        b"third.order.sync.queue": [_message(tenant)],
        b"third.order.sync.notify.queue": [],
    }


def test_redis_scope_counts_only_in_tenant_messages() -> None:
    client = _StreamClient(_streams(TENANT))
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    result = source.inspect_streams(ORDER)

    matches = {item["stream"]: item["matches"] for item in result}
    assert matches["third.order.sync.queue"] == 1


def test_redis_scope_excludes_wrong_tenant_messages() -> None:
    client = _StreamClient(_streams("TENANT-EVIL"))
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    result = source.inspect_streams(ORDER)

    matches = {item["stream"]: item["matches"] for item in result}
    assert matches["third.order.sync.queue"] == 0


def test_redis_scope_excludes_messages_without_verifiable_tenant() -> None:
    client = _StreamClient(_streams(None))
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    result = source.inspect_streams(ORDER)

    matches = {item["stream"]: item["matches"] for item in result}
    assert matches["third.order.sync.queue"] == 0


def test_redis_no_scope_predicate_matches_by_order_no_only() -> None:
    client = _StreamClient(_streams(None))
    source, _ = _source(client)

    result = source.inspect_streams(ORDER)

    matches = {item["stream"]: item["matches"] for item in result}
    assert matches["third.order.sync.queue"] == 1


def test_redis_scope_uses_bounded_reads_and_whitelist_streams() -> None:
    client = _StreamClient(_streams(TENANT))
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))
    settings = Settings.from_env()
    settings.redis.password = "secret"
    expected_max = settings.safety.redis_max_messages

    source.inspect_streams(ORDER)

    assert {stream for stream, _ in client.calls} == {
        b"third.order.sync.queue",
        b"third.order.sync.notify.queue",
    }
    assert all(count == expected_max for _, count in client.calls)


def test_redis_scope_does_not_return_raw_message_bodies() -> None:
    client = _StreamClient(_streams(TENANT))
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    result = source.inspect_streams(ORDER)

    serialized = str(result)
    assert "orderNo" not in serialized
    assert ORDER not in serialized


def test_redis_whitelist_rejects_non_stream_type_safely() -> None:
    client = _StreamClient({})
    source, _ = _source(client, QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    result = source.inspect_streams(ORDER)

    for item in result:
        assert item["type"] == "none"
        assert item["length"] == 0
        assert item["matches"] == 0


def test_order_in_scope_predicate_accepts_tenant_field() -> None:
    predicate = make_redis_order_in_scope(QueryScope(tenant_id=TENANT, site_ids=None, user_id=None))

    assert predicate({b"tenantId": TENANT.encode("utf-8")}) is True
    assert predicate({b"tenant_id": TENANT.encode("utf-8")}) is True
    assert predicate({b"tenantId": b"TENANT-EVIL"}) is False
    assert predicate({b"orderNo": ORDER.encode("utf-8")}) is False


def _caller_visibility(tenant_id: str) -> TenantVisibility:
    """把 scope 租户表达成共享行级定义的 caller profile 输入。"""
    tenant = normalize_tenant(tenant_id)
    return TenantVisibility(
        profile=VisibilityProfile.CALLER,
        allowed=frozenset({tenant}) if tenant else frozenset(),
    )


@pytest.mark.parametrize(
    "raw_tenant",
    [
        TENANT,
        f" {TENANT} ",
        TENANT.encode(),
        f" {TENANT} ".encode(),
        "TENANT-EVIL",
        b"TENANT-EVIL",
        "",
        b"",
        "   ",
        None,
        7,
    ],
)
def test_redis_scope_predicate_agrees_with_the_shared_row_rule(raw_tenant: object) -> None:
    """Redis 谓词必须与共享行级定义对同一消息给出同一结论。

    覆盖带空白与 bytes 形态的租户标识。迁移前 Redis 自己比 `tenantId` /
    `tenant_id`：用只解 bytes、不 strip 的解码直接与 `scope.tenant_id` 原值比较，
    是行级规则之外的第四套归一化——带空白的 scope 租户与干净的消息租户本属同一
    租户，却被判为范围外。这里把消息的租户字段投影成候选行，直接与
    `visible_orders` 的结论对齐。
    """
    scope = QueryScope(tenant_id=f" {TENANT} ", site_ids=None, user_id=None)
    predicate = make_redis_order_in_scope(scope)
    visibility = _caller_visibility(scope.tenant_id)

    expected = bool(visible_orders([{"tenant_id": raw_tenant}], visibility).rows)
    assert predicate({b"tenantId": raw_tenant}) is expected


def test_redis_scope_predicate_accepts_every_tenant_field_spelling() -> None:
    """str 键与 bytes 键、camelCase 与 snake_case 都是同一个租户字段。"""
    scope = QueryScope(tenant_id=TENANT, site_ids=None, user_id=None)
    predicate = make_redis_order_in_scope(scope)

    assert predicate({b"tenantId": TENANT}) is True
    assert predicate({"tenant_id": TENANT}) is True
    assert predicate({b"tenantId": f" {TENANT} "}) is True
    # 任一租户字段命中即算范围内（与迁移前一致），缺租户字段则不可验证。
    assert predicate({b"tenantId": b"TENANT-EVIL", b"tenant_id": TENANT}) is True
    assert predicate({b"orderNo": ORDER}) is False


def test_redis_scope_does_not_push_site_or_user_scope_down() -> None:
    """站点/用户范围不下推 Redis：订单已通过 MySQL scope 约束即可安全关联。"""
    scope = QueryScope(tenant_id=TENANT, site_ids=("SITE-A-1",), user_id="U-1")
    predicate = make_redis_order_in_scope(scope)

    assert predicate({b"tenantId": TENANT}) is True
    assert predicate({b"tenantId": b"TENANT-EVIL"}) is False


def test_order_in_scope_none_predicate_accepts_all() -> None:
    assert _order_in_scope(None, {b"anything": b"value"}) is True
    assert _order_in_scope(lambda fields: False, {b"a": b"b"}) is False
