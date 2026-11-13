"""The single authoritative tenant-visibility rule, with two renderings.

"Which orders may this caller see" is one business rule on the read-only
diagnostic boundary (ADR-0001). It used to be implemented six independent
times — two entry guards, the agent tool layer, and three data sources — and
had already drifted: the same order could reach different conclusions about
visibility, with different failure reasons, depending on the entry point.

This module defines the rule once and renders it twice:

- :func:`visible_orders` is the row-level rendering, for callers that hold rows
  in memory (the fixture source, the agent tool layer, the management API).
- :func:`scope_where_sql` is the parameter-bound ``WHERE`` rendering, for
  callers that push the rule down into SQL (the scoped direct sources).

The row-level and SQL renderings must both exist because the convergence point
is the *rule*, not the execution point: some callers filter rows they already
hold, others push the predicate into the query. The retired device entry
(``/v1/runs`` over ``/diag/*`` HTTP) once carried a third, entry-side rendering
(``resolve_device_tenant``); it left with the device path itself, and the
remaining surfaces all resolve their tenant up front as callers.

Only pure functions and frozen values live here, following the ``rules.py``
precedent: no class hierarchy, no runtime state, no third-party dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Reported in place of a tenant id when a row carries none. A row that cannot
#: state its tenant cannot be shown to be in scope, so it is blocked (fail
#: closed) and reported under this marker rather than crashing the caller.
UNKNOWN_TENANT = "<unknown>"


def normalize_tenant(value: object) -> str | None:
    """The one normalization for the tenant dimension.

    Strips surrounding whitespace and decodes bytes, so that a tenant taken
    from a request, a tenant stored in a row,
    and a tenant bound into SQL all compare equal regardless of how they
    arrived. Returns ``None`` for anything that cannot carry a usable tenant —
    absent, empty, blank, or a non-string type — so callers never have to
    guard the comparison themselves.

    Exactly one implementation of this exists on purpose: the drift this module
    removes started as three normalization rules that disagreed about stripping
    and about decoding ``bytes``.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


#: The evidence-journal ``source`` the row-level rendering records when an order
#: is blocked for belonging to a tenant outside the authorized set. One spelling
#: on purpose: the tool layer writes it and every surface reads it, so a producer
#: and a consumer can never drift on the string the way the six implementations
#: drifted on the rule itself.
TENANT_SCOPE_SOURCE = "harness:tenant_scope"


def tenant_matches(subject: object, expected: object) -> bool:
    """Do these two values name the same tenant?

    The comparison itself lives here for the same reason :func:`normalize_tenant`
    does: it is the tenant dimension, and a source that writes its own
    ``a != b`` re-derives the rule (stripping, decoding, absent-vs-blank) and
    eventually disagrees with the rest of the system. Both sides go through
    :func:`normalize_tenant`, so an absent tenant never equals a blank one and
    a ``bytes`` tenant equals its decoded twin.

    This is a *comparison* helper, not a visibility decision: an authorization
    guard still asks :func:`caller_visibility`, and an order gate still asks
    :func:`visible_orders`. What it removes is the third, ad-hoc copy of "are
    these the same tenant" that a configuration surface would otherwise grow.
    """
    return normalize_tenant(subject) == normalize_tenant(expected) is not None


@dataclass(frozen=True, slots=True)
class TenantVisibility:
    """A frozen scope decision input.

    ``allowed`` is the set of normalized tenant ids the caller may see. An
    empty set is meaningful: it means no tenant is permitted.
    """

    allowed: frozenset[str]

    def __post_init__(self) -> None:
        if any(normalize_tenant(item) != item for item in self.allowed):
            raise ValueError("allowed tenants must already be normalized")

    @property
    def empty(self) -> bool:
        return not self.allowed


def caller_visibility(tenant: object) -> TenantVisibility:
    """The CALLER profile for one already-resolved tenant.

    Every consumer that knows its tenant up front — the scoped direct sources,
    the Redis predicate and the tool layer's scope branch — builds the same
    profile from the same value, so the construction lives here once. A tenant
    that cannot be normalized is not a usable identity: the scope is empty,
    meaning nothing is visible, rather than a blank value bound to match. That
    is also what the SQL rendering produces (``1=0``), so the two renderings of
    one rule cannot disagree about it.
    """
    normalized = normalize_tenant(tenant)
    return TenantVisibility(allowed=frozenset({normalized}) if normalized else frozenset())


@dataclass(frozen=True, slots=True)
class VisibilityResult:
    """The outcome of applying the rule to one candidate set of rows.

    ``blocked_tenants`` is sorted and deduplicated so that every surface
    reports the same reason in the same order — the drift being removed
    included three different failure vocabularies for one root cause.
    """

    rows: tuple[Mapping[str, Any], ...]
    blocked_tenants: tuple[str, ...]


def visible_orders(rows: Sequence[Mapping[str, Any]], visibility: TenantVisibility) -> VisibilityResult:
    """Apply the rule row by row.

    Decides per row, which is the only policy that composes with the callers
    that need it: the fixture source drops invisible rows, while the agent tool
    layer turns a non-empty ``blocked_tenants`` into an all-or-nothing blocked
    evidence entry. Both are renderings of this one result, not separate rules.
    """
    visible: list[Mapping[str, Any]] = []
    blocked: set[str] = set()
    for row in rows:
        tenant = normalize_tenant(row.get("tenant_id"))
        if tenant is None:
            blocked.add(UNKNOWN_TENANT)
        elif tenant in visibility.allowed:
            visible.append(row)
        else:
            blocked.add(tenant)
    return VisibilityResult(rows=tuple(visible), blocked_tenants=tuple(sorted(blocked)))


def scope_where_sql(
    column: str, visibility: TenantVisibility, *, placeholder: str = "?"
) -> tuple[str, list[str]]:
    """Render the same rule as a parameter-bound SQL predicate.

    Returns ``(predicate, params)``. The tenant is never interpolated into the
    predicate — it is bound, matching the existing scope push-down. An empty
    scope renders as the constant ``("1=0", [])`` so a caller can decide not to
    issue the query at all, matching the existing "empty site scope
    short-circuits and issues no SQL" semantics.

    ``placeholder`` is the driver's parameter marker (``?`` for sqlite3, ``%s``
    for pymysql) and carries no meaning of its own: the visibility rule is the
    predicate and the bound values, which are identical either way.
    """
    if visibility.empty:
        return "1=0", []
    tenants = sorted(visibility.allowed)
    if len(tenants) == 1:
        return f"{column}={placeholder}", [tenants[0]]
    placeholders = ", ".join(placeholder for _ in tenants)
    return f"{column} IN ({placeholders})", list(tenants)
