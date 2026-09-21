"""Tenant visibility must be one definition with two renderings (#325 T1).

The rule "which orders may this run see" was implemented six independent times
across the entry guards, the agent tool layer and three data sources, and had
already drifted. These tests pin the shared definition itself, and the
equivalence between its row-level rendering and its SQL rendering.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.order_visibility import (
    UNKNOWN_TENANT,
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    scope_where_sql,
    visible_orders,
)


def _visibility(profile: VisibilityProfile, allowed: set[str] | None) -> TenantVisibility:
    return TenantVisibility(profile=profile, allowed=None if allowed is None else frozenset(allowed))


# --- normalization: defined once -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  tenant-a  ", "tenant-a"),
        (b"tenant-a", "tenant-a"),
        ("tenant-a", "tenant-a"),
        ("", None),
        ("   ", None),
        (None, None),
        (b"", None),
        (123, None),
    ],
)
def test_normalize_tenant_is_total(raw: object, expected: str | None) -> None:
    # One normalization for the tenant dimension: strip, decode bytes, and treat
    # anything unusable as absent rather than raising.
    assert normalize_tenant(raw) == expected


def test_normalize_tenant_matches_parse_request_stripping() -> None:
    # parse_request strips request.tenant_id; the allowed set must strip too or
    # a padded identifier makes the two disagree and the filter falsely rejects.
    assert normalize_tenant(" tenant-a ") == normalize_tenant("tenant-a")


# --- row-level rendering ---------------------------------------------------------


def test_unrestricted_profile_blocks_nothing() -> None:
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}, {"order_no": "o2", "tenant_id": "tenant-b"}]
    result = visible_orders(rows, _visibility(VisibilityProfile.DEVICE, None))
    assert [r["order_no"] for r in result.rows] == ["o1", "o2"]
    assert result.blocked_tenants == ()


def test_out_of_scope_rows_are_reported_not_silently_dropped() -> None:
    rows = [{"order_no": "o1", "tenant_id": "TENANT-DEMO"}]
    result = visible_orders(rows, _visibility(VisibilityProfile.CALLER, {"tenant-a"}))
    assert result.rows == ()
    assert result.blocked_tenants == ("TENANT-DEMO",)


def test_scope_is_decided_per_row() -> None:
    rows = [
        {"order_no": "o1", "tenant_id": "in"},
        {"order_no": "o2", "tenant_id": "out"},
        {"order_no": "o3", "tenant_id": " in "},
    ]
    result = visible_orders(rows, _visibility(VisibilityProfile.CALLER, {"in"}))
    assert [r["order_no"] for r in result.rows] == ["o1", "o3"]
    assert result.blocked_tenants == ("out",)


def test_byte_and_padded_tenants_compare_equal_to_plain_ones() -> None:
    rows = [{"order_no": "o1", "tenant_id": b"tenant-a"}, {"order_no": "o2", "tenant_id": " tenant-a "}]
    result = visible_orders(rows, _visibility(VisibilityProfile.DEVICE, {"tenant-a"}))
    assert [r["order_no"] for r in result.rows] == ["o1", "o2"]
    assert result.blocked_tenants == ()


def test_row_without_a_tenant_fails_closed() -> None:
    # Previously this crashed: the blocked set could contain None, and
    # sorted({None, "a"}) raises TypeError. Absent tenant is unusable for a
    # scope decision, so it is blocked and reported, never visible.
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}, {"order_no": "o2"}]
    result = visible_orders(rows, _visibility(VisibilityProfile.CALLER, {"tenant-a"}))
    assert [r["order_no"] for r in result.rows] == ["o1"]
    assert result.blocked_tenants == (UNKNOWN_TENANT,)


def test_empty_scope_short_circuits_to_nothing_visible() -> None:
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}]
    result = visible_orders(rows, _visibility(VisibilityProfile.CALLER, set()))
    assert result.rows == ()
    assert result.blocked_tenants == ("tenant-a",)


def test_blocked_tenants_are_sorted_and_deduplicated() -> None:
    rows = [{"tenant_id": "b"}, {"tenant_id": "a"}, {"tenant_id": "b"}, {"tenant_id": "a"}]
    result = visible_orders(rows, _visibility(VisibilityProfile.CALLER, {"z"}))
    assert result.blocked_tenants == ("a", "b")


# --- one definition, two renderings ----------------------------------------------
#
# The row-level rendering above and the SQL rendering below are two views of one
# definition. The equivalence is pinned twice: here against the rendering itself,
# and in ``tests/test_mysql_scope.py`` against the real ``MySQLSource._scope_where``
# output, which is the only production consumer of the SQL rendering.


def _visible_via_sql(rows: list[dict[str, object]], visibility) -> list[dict[str, object]]:
    """Apply the SQL rendering in Python so both renderings can be compared.

    This is deliberately a separate, independent evaluation of the rendered
    predicate: if the SQL rendering ever diverges from the row-level rule, this
    disagrees and the equivalence test fails.
    """
    predicate, params = scope_where_sql("tenant_id", visibility)
    assert predicate in {"1=1", "1=0", "tenant_id=?"} or predicate.startswith("tenant_id IN (")
    if predicate == "1=1":
        return list(rows)
    if predicate == "1=0":
        return []
    if predicate == "tenant_id=?":
        return [r for r in rows if normalize_tenant(r.get("tenant_id")) == params[0]]
    allowed = set(params)
    return [r for r in rows if normalize_tenant(r.get("tenant_id")) in allowed]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"order_no": "o1", "tenant_id": "tenant-a"}],
        [{"order_no": "o1", "tenant_id": "other"}],
        [{"order_no": "o1", "tenant_id": b"tenant-a"}, {"order_no": "o2", "tenant_id": " tenant-a "}],
        [{"order_no": "o1"}, {"order_no": "o2", "tenant_id": "tenant-a"}],
        [{"order_no": "o1", "tenant_id": "a"}, {"order_no": "o2", "tenant_id": "b"}],
    ],
)
@pytest.mark.parametrize(
    "profile,allowed",
    [
        (VisibilityProfile.CALLER, {"tenant-a"}),
        (VisibilityProfile.CALLER, set()),
        (VisibilityProfile.CALLER, {"a", "b"}),
        # None is only valid for the device profile: an unbound registration.
        (VisibilityProfile.DEVICE, None),
        (VisibilityProfile.DEVICE, {"tenant-a"}),
    ],
)
def test_sql_rendering_and_row_rule_agree(
    rows: list, profile: VisibilityProfile, allowed: set[str] | None
) -> None:
    visibility = _visibility(profile, allowed)
    row_result = visible_orders(rows, visibility)
    sql_result = _visible_via_sql(rows, visibility)
    assert [r.get("order_no") for r in row_result.rows] == [r.get("order_no") for r in sql_result]


def test_sql_rendering_never_interpolates_the_tenant() -> None:
    # Parameter-bound, like the existing MySQL scope push-down.
    predicate, params = scope_where_sql("tenant_id", _visibility(VisibilityProfile.CALLER, {"a'b"}))
    assert "'" not in predicate
    assert "a'b" in params


def test_unrestricted_profile_renders_a_tautology() -> None:
    predicate, params = scope_where_sql("tenant_id", _visibility(VisibilityProfile.DEVICE, None))
    assert predicate == "1=1"
    assert params == []


def test_empty_scope_renders_a_contradiction_without_querying() -> None:
    predicate, params = scope_where_sql("tenant_id", _visibility(VisibilityProfile.CALLER, set()))
    assert predicate == "1=0"
    assert params == []


def test_the_placeholder_is_the_driver_marker_not_part_of_the_rule() -> None:
    # The visibility rule is the predicate plus the bound values; the marker is
    # whichever paramstyle the driver speaks (? for sqlite3, %s for pymysql).
    visibility = _visibility(VisibilityProfile.CALLER, {"a", "b"})
    sqlite_predicate, sqlite_params = scope_where_sql("tenant_id", visibility)
    mysql_predicate, mysql_params = scope_where_sql("tenant_id", visibility, placeholder="%s")
    assert sqlite_predicate == "tenant_id IN (?, ?)"
    assert mysql_predicate == "tenant_id IN (%s, %s)"
    assert sqlite_params == mysql_params == ["a", "b"]


# --- consumer: fixture data source ------------------------------------------------


def test_fixture_source_uses_the_shared_rule(tmp_path) -> None:
    from aiops_diagnostics.sources import FixtureSources

    payload = {
        "orders": [
            {"order_no": "o1", "tenant_id": "TENANT-DEMO"},
            {"order_no": "o2", "tenant_id": "other"},
        ]
    }
    path = tmp_path / "f.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    source = FixtureSources(path)

    # No tenant filter: unchanged, both visible (tenant discovery).
    assert len(source.get_orders("o1")) == 1
    # In-scope tenant: visible.
    assert [r["order_no"] for r in source.get_orders("o1", "TENANT-DEMO")] == ["o1"]
    # Out-of-scope tenant: invisible, and never leaked.
    assert source.get_orders("o1", "tenant-a") == []
    # Padded tenant still matches: the shared normalization strips.
    assert [r["order_no"] for r in source.get_orders("o1", " TENANT-DEMO ")] == ["o1"]
