"""Tenant visibility must be one definition with two renderings (#325).

The rule "which orders may this caller see" was implemented six independent
times across the entry guards, the agent tool layer and three data sources,
and had already drifted. These tests pin the shared definition itself, the
equivalence between its row-level rendering and its SQL rendering, and the
source-level guard that keeps a comparison from growing back outside the
shared module. (A third, entry-side rendering left with the retired device
path: ``resolve_device_tenant`` only served ``/v1/runs``.)
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aiops_diagnostics.identity.order_visibility import (
    UNKNOWN_TENANT,
    TenantVisibility,
    caller_visibility,
    normalize_tenant,
    scope_where_sql,
    visible_orders,
)

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"


def _visibility(allowed: set[str]) -> TenantVisibility:
    return TenantVisibility(allowed=frozenset(allowed))


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


#: The modules scanned for a tenant comparison growing back: the data sources
#: and the gateway surfaces that consume the rule. A comparison written back
#: into any of them is how the six implementations drifted apart — the same
#: order reaching different conclusions, with different failure reasons,
#: depending on the entry point.
#:
#: Other code that names a tenant compares something else and is deliberately
#: left alone: ``knowledge_retrieval`` checks a media grant's tenant, and
#: ``scope_context._effective_tenant`` binds the caller/subject/requested
#: tenant. Neither decides which orders a caller may see, so routing them
#: through this rule would be wrong, not thorough.
VISIBILITY_MODULES = (
    "data/sources.py",
    "gateway/gateway_api.py",
    "gateway/gateway_runtime.py",
)


def _names_a_tenant(node: ast.AST) -> bool:
    """Does this expression reach a tenant value at all?

    ``row.get("tenant_id")`` (a row read) and ``binding.tenant_id`` (a caller
    attribute) are the spellings a comparison can reach a tenant through; that
    comparison is the rendering's job and may not appear outside the shared
    module.
    """
    return any(
        (isinstance(sub, ast.Attribute) and sub.attr == "tenant_id")
        or (isinstance(sub, ast.Constant) and sub.value == "tenant_id")
        for sub in ast.walk(node)
    )


def test_no_module_renders_its_own_tenant_comparison() -> None:
    """Tenant visibility is compared in exactly one place.

    Source-level rather than behavioural, because what is being prevented is a
    future edit. The convergence only holds while every consumer asks the shared
    rule; a comparison written back into a source, the tool layer or an entry
    guard is how one order came to mean two things at once. The failure names the
    file and the line so the offender is obvious.
    """
    offenders: list[str] = []
    for name in VISIBILITY_MODULES:
        tree = ast.parse((SOURCE_ROOT / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and _names_a_tenant(node):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "tenant visibility is one definition in order_visibility.py; an independent "
        f"tenant comparison was found at {', '.join(offenders)}"
    )


# --- row-level rendering ---------------------------------------------------------


def test_out_of_scope_rows_are_reported_not_silently_dropped() -> None:
    rows = [{"order_no": "o1", "tenant_id": "TENANT-DEMO"}]
    result = visible_orders(rows, _visibility({"tenant-a"}))
    assert result.rows == ()
    assert result.blocked_tenants == ("TENANT-DEMO",)


def test_scope_is_decided_per_row() -> None:
    rows = [
        {"order_no": "o1", "tenant_id": "in"},
        {"order_no": "o2", "tenant_id": "out"},
        {"order_no": "o3", "tenant_id": " in "},
    ]
    result = visible_orders(rows, _visibility({"in"}))
    assert [r["order_no"] for r in result.rows] == ["o1", "o3"]
    assert result.blocked_tenants == ("out",)


def test_byte_and_padded_tenants_compare_equal_to_plain_ones() -> None:
    rows = [{"order_no": "o1", "tenant_id": b"tenant-a"}, {"order_no": "o2", "tenant_id": " tenant-a "}]
    result = visible_orders(rows, _visibility({"tenant-a"}))
    assert [r["order_no"] for r in result.rows] == ["o1", "o2"]
    assert result.blocked_tenants == ()


def test_row_without_a_tenant_fails_closed() -> None:
    # Previously this crashed: the blocked set could contain None, and
    # sorted({None, "a"}) raises TypeError. Absent tenant is unusable for a
    # scope decision, so it is blocked and reported, never visible.
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}, {"order_no": "o2"}]
    result = visible_orders(rows, _visibility({"tenant-a"}))
    assert [r["order_no"] for r in result.rows] == ["o1"]
    assert result.blocked_tenants == (UNKNOWN_TENANT,)


def test_empty_scope_short_circuits_to_nothing_visible() -> None:
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}]
    result = visible_orders(rows, _visibility(set()))
    assert result.rows == ()
    assert result.blocked_tenants == ("tenant-a",)


def test_the_caller_scope_has_one_construction() -> None:
    """Every consumer that knows its tenant builds the same scope.

    The scoped direct sources, the Redis predicate, the tool layer's scope branch
    and the fixture source each used to assemble this themselves, and one of them
    assembled it wrongly (a blank tenant became ``""`` in the allowed set, which
    ``TenantVisibility`` rejects). One constructor means there is nothing left to
    assemble wrongly.
    """
    assert caller_visibility("tenant-a").allowed == frozenset({"tenant-a"})
    assert caller_visibility(" tenant-a ").allowed == frozenset({"tenant-a"})
    # A tenant that cannot be normalized is not a usable identity: nothing is
    # visible, which is what the SQL rendering produces too.
    for unusable in (None, "", "   ", 123):
        assert caller_visibility(unusable).allowed == frozenset()


def test_blocked_tenants_are_sorted_and_deduplicated() -> None:
    rows = [{"tenant_id": "b"}, {"tenant_id": "a"}, {"tenant_id": "b"}, {"tenant_id": "a"}]
    result = visible_orders(rows, _visibility({"z"}))
    assert result.blocked_tenants == ("a", "b")


# --- one definition, two renderings ----------------------------------------------
#
# The row-level rendering above and the SQL rendering below are two views of one
# definition. The equivalence is pinned twice: here against the rendering itself,
# and in ``tests/data/test_mysql_scope.py`` against the real ``MySQLSource._scope_where``
# output, which is the only production consumer of the SQL rendering.


def _visible_via_sql(rows: list[dict[str, object]], visibility) -> list[dict[str, object]]:
    """Apply the SQL rendering in Python so both renderings can be compared.

    This is deliberately a separate, independent evaluation of the rendered
    predicate: if the SQL rendering ever diverges from the row-level rule, this
    disagrees and the equivalence test fails.
    """
    predicate, params = scope_where_sql("tenant_id", visibility)
    assert predicate in {"1=0", "tenant_id=?"} or predicate.startswith("tenant_id IN (")
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
@pytest.mark.parametrize("allowed", [{"tenant-a"}, set(), {"a", "b"}])
def test_sql_rendering_and_row_rule_agree(rows: list, allowed: set[str]) -> None:
    visibility = _visibility(allowed)
    row_result = visible_orders(rows, visibility)
    sql_result = _visible_via_sql(rows, visibility)
    assert [r.get("order_no") for r in row_result.rows] == [r.get("order_no") for r in sql_result]


def test_sql_rendering_never_interpolates_the_tenant() -> None:
    # Parameter-bound, like the existing MySQL scope push-down.
    predicate, params = scope_where_sql("tenant_id", _visibility({"a'b"}))
    assert "'" not in predicate
    assert "a'b" in params


def test_empty_scope_renders_a_contradiction_without_querying() -> None:
    predicate, params = scope_where_sql("tenant_id", _visibility(set()))
    assert predicate == "1=0"
    assert params == []


def test_the_placeholder_is_the_driver_marker_not_part_of_the_rule() -> None:
    # The visibility rule is the predicate plus the bound values; the marker is
    # whichever paramstyle the driver speaks (? for sqlite3, %s for pymysql).
    visibility = _visibility({"a", "b"})
    sqlite_predicate, sqlite_params = scope_where_sql("tenant_id", visibility)
    mysql_predicate, mysql_params = scope_where_sql("tenant_id", visibility, placeholder="%s")
    assert sqlite_predicate == "tenant_id IN (?, ?)"
    assert mysql_predicate == "tenant_id IN (%s, %s)"
    assert sqlite_params == mysql_params == ["a", "b"]


# --- consumer: fixture data source ------------------------------------------------


def test_fixture_source_uses_the_shared_rule(tmp_path: Path) -> None:
    from aiops_diagnostics.data.sources import FixtureSources

    payload = {
        "orders": [
            {"order_no": "o1", "tenant_id": "TENANT-DEMO"},
            {"order_no": "o2", "tenant_id": "other"},
        ]
    }
    path = tmp_path / "f.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    source = FixtureSources(path)

    # No tenant filter: unchanged, both visible (tenant discovery).
    assert len(source.get_orders("o1")) == 1
    # In-scope tenant: visible.
    assert [r["order_no"] for r in source.get_orders("o1", "TENANT-DEMO")] == ["o1"]
    # Out-of-scope tenant: invisible, and never leaked.
    assert source.get_orders("o1", "tenant-a") == []
    # Padded tenant still matches: the shared normalization strips.
    assert [r["order_no"] for r in source.get_orders("o1", " TENANT-DEMO ")] == ["o1"]


def _order_fixture(tmp_path: Path) -> Path:
    """A fixture whose order, fee record, occupy order and device all state a tenant."""
    payload = {
        "orders": [{"order_no": "o1", "tenant_id": "TENANT-DEMO"}],
        "fee_template_records": {"o1": {"order_no": "o1", "tenant_id": "TENANT-DEMO"}},
        "occupy_orders": [
            {"orderId": "1", "order_no": "o1", "tenant_id": "TENANT-DEMO"},
            {"orderId": "2", "order_no": "o1", "tenant_id": "other"},
        ],
        "devices": [{"id": "D-1", "device_code": "C-1", "tenant_id": "TENANT-DEMO"}],
    }
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_fixture_source_filters_every_lookup_through_the_shared_rule(tmp_path: Path) -> None:
    """Every fixture lookup asks the shared rule, not just ``get_orders``.

    ``get_orders`` was migrated first; the order's fee record, its occupy orders
    and the device lookup kept comparing tenants by hand, so a padded identifier
    matched on one lookup and missed on the others — the drift this PRD removes,
    inside one source. They now all go through the same row-level rendering.
    """
    from aiops_diagnostics.data.sources import FixtureSources

    source = FixtureSources(_order_fixture(tmp_path))

    assert [r["order_no"] for r in source.get_orders("o1", " TENANT-DEMO ")] == ["o1"]
    assert source.get_device("D-1", None, " TENANT-DEMO ")["id"] == "D-1"
    assert source.get_device("D-1", None, "other") is None


def test_a_blank_tenant_sees_nothing_rather_than_everything(tmp_path: Path) -> None:
    """A caller that names a tenant it cannot use is not a caller with no tenant.

    Two inputs, two meanings, and the difference is fail-closed versus fail-open:

    - ``tenant_id=None`` names no tenant at all — the discovery mode the
      fixture source keeps as a deliberate no-filter bypass;
    - ``tenant_id=""`` / ``"   "`` names one that cannot be normalized — an
      unusable identity, which ``caller_visibility`` renders as the *empty*
      scope and ``scope_where_sql`` renders as ``1=0``.

    Reading the second as the first is the fail-open direction in the one filter
    this module converges, and it makes the row-level and SQL renderings disagree
    about the identical input. It is also what the last commit of this change did
    by accident: it wrote ``_caller_visible``'s unrestricted branch as
    ``normalize_tenant(tenant_id) is None`` and pinned that behavior here, while
    the same commit's stated intent was "blank renders as nothing visible".
    """
    from aiops_diagnostics.data.sources import FixtureSources

    source = FixtureSources(_order_fixture(tmp_path))

    # No tenant named: the discovery mode an unbound registration uses.
    assert [r["order_no"] for r in source.get_orders("o1", None)] == ["o1"]
    assert source.get_device("D-1", None, None)["id"] == "D-1"

    # Named but unusable: nothing is visible, on every lookup.
    for blank in ("", "   "):
        assert source.get_orders("o1", blank) == []
        assert source.get_device("D-1", None, blank) is None
