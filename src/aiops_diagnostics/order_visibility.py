"""The single authoritative tenant-visibility rule, with two renderings.

"Which orders may this run see" is one business rule on the read-only
diagnostic boundary (ADR-0001). It used to be implemented six independent
times — two entry guards, the agent tool layer, and three data sources — and
had already drifted: the same order could reach different conclusions about
visibility, with different failure reasons, depending on the entry point.

This module defines the rule once and renders it twice:

- :func:`visible_orders` is the row-level rendering, for callers that hold rows
  in memory (the fixture source, the agent tool layer).
- :func:`scope_where_sql` is the parameter-bound ``WHERE`` rendering, for
  callers that push the rule down into SQL (the scoped direct sources).

The device path has no SQL to push down (it reads orders over ``/diag/*`` HTTP)
while the caller path does; that is why the convergence point is the *rule*
rather than the execution point, and why both renderings must exist.

Only pure functions and frozen values live here, following the ``rules.py``
precedent: no class hierarchy, no runtime state, no third-party dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Reported in place of a tenant id when a row carries none. A row that cannot
#: state its tenant cannot be shown to be in scope, so it is blocked (fail
#: closed) and reported under this marker rather than crashing the caller.
UNKNOWN_TENANT = "<unknown>"


class VisibilityProfile(StrEnum):
    """The two named scope profiles. The differences are explicit, not implicit.

    ``DEVICE`` is the device/workspace registration profile. A registration may
    carry no tenant at all, so ``allowed=None`` is a *valid* state meaning
    "discover the tenant from the order rows and block nothing"; a registered
    device supplies its authorized set and is enforced against it.

    ``CALLER`` is the profile resolved from a
    :class:`~aiops_diagnostics.query_scope.QueryScope`: the tenant is always
    known up front, so ``allowed=None`` is a programming error rather than a
    mode. An empty set is meaningful here — it means nothing is visible.
    """

    DEVICE = "device"
    CALLER = "caller"


def normalize_tenant(value: object) -> str | None:
    """The one normalization for the tenant dimension.

    Strips surrounding whitespace and decodes bytes, so that a tenant taken
    from a request (which ``parse_request`` strips), a tenant stored in a row,
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


@dataclass(frozen=True, slots=True)
class TenantVisibility:
    """A frozen scope decision input.

    ``allowed`` is a set of normalized tenant ids, or ``None`` for the
    unrestricted ``DEVICE`` profile. An empty set is meaningful and must not be
    conflated with ``None``: it means no tenant is permitted.
    """

    profile: VisibilityProfile
    allowed: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.profile is VisibilityProfile.CALLER and self.allowed is None:
            raise ValueError("the caller profile always knows its tenant; allowed is required")
        if self.allowed is not None and any(normalize_tenant(item) != item for item in self.allowed):
            raise ValueError("allowed tenants must already be normalized")

    @property
    def unrestricted(self) -> bool:
        return self.allowed is None

    @property
    def empty(self) -> bool:
        return self.allowed is not None and not self.allowed


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
    if visibility.unrestricted:
        return VisibilityResult(rows=tuple(rows), blocked_tenants=())
    allowed = visibility.allowed or frozenset()
    visible: list[Mapping[str, Any]] = []
    blocked: set[str] = set()
    for row in rows:
        tenant = normalize_tenant(row.get("tenant_id"))
        if tenant is None:
            blocked.add(UNKNOWN_TENANT)
        elif tenant in allowed:
            visible.append(row)
        else:
            blocked.add(tenant)
    return VisibilityResult(rows=tuple(visible), blocked_tenants=tuple(sorted(blocked)))


def scope_where_sql(column: str, visibility: TenantVisibility) -> tuple[str, list[str]]:
    """Render the same rule as a parameter-bound SQL predicate.

    Returns ``(predicate, params)``. The tenant is never interpolated into the
    predicate — it is bound, matching the existing scope push-down. The two
    degenerate profiles render as constants so a caller can decide not to issue
    the query at all:

    - unrestricted -> ``("1=1", [])``
    - empty scope  -> ``("1=0", [])``, matching the existing "empty site scope
      short-circuits and issues no SQL" semantics.
    """
    if visibility.unrestricted:
        return "1=1", []
    if visibility.empty:
        return "1=0", []
    tenants = sorted(visibility.allowed or ())
    if len(tenants) == 1:
        return f"{column} = ?", [tenants[0]]
    placeholders = ", ".join("?" for _ in tenants)
    return f"{column} IN ({placeholders})", list(tenants)
