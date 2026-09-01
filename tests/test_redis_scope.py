from __future__ import annotations

from typing import Any

from aiops_diagnostics.config import Settings
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


def test_order_in_scope_none_predicate_accepts_all() -> None:
    assert _order_in_scope(None, {b"anything": b"value"}) is True
    assert _order_in_scope(lambda fields: False, {b"a": b"b"}) is False
