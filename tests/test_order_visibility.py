"""Tenant visibility must be one definition with three renderings (#325).

The rule "which orders may this run see" was implemented six independent times
across the entry guards, the agent tool layer and three data sources, and had
already drifted. These tests pin the shared definition itself, the equivalence
between its row-level rendering and its SQL rendering, the entry rendering that
replaced the two entry guards, and the source-level guard that keeps a comparison
from growing back outside the shared module.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aiops_diagnostics.order_visibility import (
    DEVICE_TENANT_MISMATCH,
    UNKNOWN_TENANT,
    DeviceTenantError,
    TenantVisibility,
    VisibilityProfile,
    caller_visibility,
    normalize_tenant,
    resolve_device_tenant,
    scope_where_sql,
    visible_orders,
)

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"


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


# --- entry rendering -------------------------------------------------------------
#
# The device entry used to hold the rule twice: ``POST /v1/runs`` compared the
# request tenant against the enrolled one and answered 403, and the runtime's
# ``_tenant_for_device`` compared the very same two values again and answered
# 400. One request could therefore collect two status codes and two error
# vocabularies for one condition. ``resolve_device_tenant`` is the single
# definition; the HTTP layer only maps its error to a status.


def test_mismatching_request_tenant_is_refused_with_one_code() -> None:
    with pytest.raises(DeviceTenantError) as excinfo:
        resolve_device_tenant("tenant-a", "tenant-b")
    assert excinfo.value.code == DEVICE_TENANT_MISMATCH
    assert str(excinfo.value) == "requested tenant does not match the enrolled device scope"


@pytest.mark.parametrize(
    ("enrolled", "requested", "expected"),
    [
        ("tenant-a", "tenant-a", "tenant-a"),
        ("tenant-a", None, "tenant-a"),
        # A blank request tenant is an absent one, not a second tenant to match.
        ("tenant-a", "", "tenant-a"),
        ("tenant-a", "   ", "tenant-a"),
        # Padded on either side: one normalization, so both forms are the tenant.
        (" tenant-a ", "tenant-a", "tenant-a"),
        ("tenant-a", " tenant-a ", "tenant-a"),
        # A workspace-level registration binds no tenant: the request decides,
        # and the tenant is discovered from the order rows.
        (None, "tenant-a", "tenant-a"),
        (None, " tenant-a ", "tenant-a"),
        (None, None, None),
        (None, "", None),
    ],
)
def test_the_effective_tenant_is_the_normalized_one(
    enrolled: str | None, requested: str | None, expected: str | None
) -> None:
    assert resolve_device_tenant(enrolled, requested) == expected


def test_the_effective_tenant_feeds_both_renderings_identically() -> None:
    # The allowed set used to come from the un-stripped effective tenant while
    # parse_request stored the stripped one, so a padded identifier made the
    # row-level filter disagree with the run's own tenant (and could not even
    # enter TenantVisibility, which requires an already-normalized set).
    effective = resolve_device_tenant(" tenant-a ", " tenant-a ")
    assert effective == normalize_tenant(effective)
    visibility = TenantVisibility(profile=VisibilityProfile.DEVICE, allowed=frozenset({effective}))
    rows = [{"order_no": "o1", "tenant_id": "tenant-a"}]
    assert [row["order_no"] for row in visible_orders(rows, visibility).rows] == ["o1"]


def test_the_device_entry_guard_is_implemented_once() -> None:
    """No second implementation of the device-entry tenant comparison.

    Source-level rather than behavioural, because what is being prevented is a
    future edit: the entry rule lives in ``order_visibility`` and the HTTP layer
    only maps its error. Re-adding a comparison of ``device.tenant_id`` in
    either of these two modules is how the duplicate status code came back.
    """
    offenders: list[str] = []
    for name in ("gateway_api.py", "gateway_runtime.py"):
        tree = ast.parse((SOURCE_ROOT / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if any(
                isinstance(sub, ast.Attribute)
                and sub.attr == "tenant_id"
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "device"
                for sub in ast.walk(node)
            ):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        f"device.tenant_id must be compared only in order_visibility.py; found: {', '.join(offenders)}"
    )


#: The modules that render the tenant-visibility rule: the two entry guards, the
#: agent tool layer, the shared agent-path preparation, and the data sources. A
#: tenant comparison growing back in any of them is how the six implementations
#: drifted apart — the same order reaching different conclusions, with different
#: failure reasons, depending on the entry point.
#:
#: Everything else that compares a tenant compares something else, and the PRD
#: deliberately leaves it alone (its Out of Scope names C/B identity mapping and
#: platform determination): ``agent_validator`` binds an agent turn to its
#: manifest's tenant, ``faq`` checks a C/B role record belongs to the effective
#: tenant, ``knowledge_retrieval`` checks a media grant's tenant,
#: ``shortcut_lifecycle`` classifies the platform content domain against its
#: ``__platform__`` sentinel, and ``scope_context._effective_tenant`` binds the
#: caller/subject/requested tenant. None of them decides which orders a run may
#: see, so routing them through this rule would be wrong, not thorough.
VISIBILITY_MODULES = (
    "sources.py",
    "diagnostic_tools.py",
    "agent_runner.py",
    "gateway_api.py",
    "gateway_runtime.py",
)


def _names_a_tenant(node: ast.AST) -> bool:
    """Does this expression reach a tenant value at all?

    ``row.get("tenant_id")`` (a row read) and ``device.tenant_id`` (an entry
    guard) are the two spellings a comparison can reach a tenant through; both
    are the rendering's job and neither may appear outside the shared module.
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


def test_the_caller_profile_has_one_construction() -> None:
    """Every consumer that knows its tenant builds the same profile.

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


def test_fixture_source_uses_the_shared_rule(tmp_path: Path) -> None:
    from aiops_diagnostics.sources import FixtureSources

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
    from aiops_diagnostics.sources import FixtureSources

    source = FixtureSources(_order_fixture(tmp_path))

    assert [r["order_no"] for r in source.get_orders("o1", " TENANT-DEMO ")] == ["o1"]
    assert source.get_fee_template_record("o1", " TENANT-DEMO ") is not None
    assert source.get_fee_template_record("o1", "other") is None
    assert [r["orderId"] for r in source.get_occupy_orders(order_id="1", tenant_id=" TENANT-DEMO ")] == ["1"]
    assert source.get_occupy_orders(order_id="1", tenant_id="other") == []
    assert source.get_device("D-1", None, " TENANT-DEMO ")["id"] == "D-1"
    assert source.get_device("D-1", None, "other") is None


def test_a_blank_tenant_constrains_nothing_instead_of_raising(tmp_path: Path) -> None:
    """A caller that supplies no usable tenant imposes no constraint.

    The first fixture migration built the allowed set as
    ``{normalize_tenant(tenant) or ""}``, and ``""`` is not a normalized tenant —
    ``TenantVisibility`` rejects it, so a blank tenant raised ``ValueError``
    instead of meaning "no filter", which is what it meant before that migration
    and what the no-scope MySQL branch still does. The shared constructor makes
    the case unrepresentable rather than wrong.
    """
    from aiops_diagnostics.sources import FixtureSources

    source = FixtureSources(_order_fixture(tmp_path))

    for blank in ("", "   "):
        assert [r["order_no"] for r in source.get_orders("o1", blank)] == ["o1"]
        assert source.get_fee_template_record("o1", blank) is not None
        assert len(source.get_occupy_orders(order_id="1", tenant_id=blank)) == 1
        assert source.get_device("D-1", None, blank)["id"] == "D-1"
