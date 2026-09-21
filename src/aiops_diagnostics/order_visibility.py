"""The single authoritative tenant-visibility rule, with three renderings.

"Which orders may this run see" is one business rule on the read-only
diagnostic boundary (ADR-0001). It used to be implemented six independent
times — two entry guards, the agent tool layer, and three data sources — and
had already drifted: the same order could reach different conclusions about
visibility, with different failure reasons, depending on the entry point.

This module defines the rule once and renders it three times:

- :func:`resolve_device_tenant` is the entry rendering: it decides the effective
  tenant of one run from the tenant an entry is authorized for and the tenant
  the request names, refusing a request that would widen that scope.
- :func:`visible_orders` is the row-level rendering, for callers that hold rows
  in memory (the fixture source, the agent tool layer).
- :func:`scope_where_sql` is the parameter-bound ``WHERE`` rendering, for
  callers that push the rule down into SQL (the scoped direct sources).

The device path has no SQL to push down (it reads orders over ``/diag/*`` HTTP)
while the caller path does; that is why the convergence point is the *rule*
rather than the execution point, and why the row-level and SQL renderings must
both exist. The entry rendering comes first in the list because it runs before
either: a run's effective tenant is decided once, here, and every later
rendering compares against that decision.

Only pure functions and frozen values live here, following the ``rules.py``
precedent — plus the one error the entry rendering raises: no class hierarchy,
no runtime state, no third-party dependency.
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


#: The evidence-journal ``source`` the row-level rendering records when an order
#: is blocked for belonging to a tenant outside the authorized set. One spelling
#: on purpose: the tool layer writes it and every surface reads it, so a producer
#: and a consumer can never drift on the string the way the six implementations
#: drifted on the rule itself.
TENANT_SCOPE_SOURCE = "harness:tenant_scope"


class DeviceTenantError(RuntimeError):
    """A run request named a tenant outside the entry's authorized scope.

    One condition, one exception type, so a transport layer maps it to exactly
    one status code and never re-decides the condition — the duplication that
    let one request answer 403 at the edge and then 400 inside the runtime. The
    type *is* the identity: the device run surface has no error-code envelope to
    put a code into (``/v1/runs`` answers ``HTTPException(403, detail=...)``), so
    a ``code`` attribute here would be an extension point with no reader.
    """


def resolve_device_tenant(enrolled: str | None, requested: str | None) -> str | None:
    """The entry rendering: decide the effective tenant, or refuse the request.

    ``enrolled`` is the tenant the entry is authorized for — for the device
    entry, the enrolled device's tenant. ``None`` is a valid registration that
    binds no tenant (workspace-level: the tenant is discovered from the order
    rows and nothing is blocked). ``requested`` is the tenant the request names;
    it may only ever *equal* the enrolled one, because a request can never widen
    the scope it runs under.

    Both sides go through :func:`normalize_tenant`, so the returned tenant is
    the same normalized form the row-level and SQL renderings compare against:
    the allowed set and the run's own tenant can no longer disagree about
    stripping. A blank or otherwise unusable requested tenant is an absent one,
    not a second tenant to match.
    """
    enrolled_tenant = normalize_tenant(enrolled)
    requested_tenant = normalize_tenant(requested)
    if enrolled_tenant is None:
        return requested_tenant
    if requested_tenant is not None and requested_tenant != enrolled_tenant:
        raise DeviceTenantError("requested tenant does not match the enrolled device scope")
    return enrolled_tenant


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
    return TenantVisibility(
        profile=VisibilityProfile.CALLER,
        allowed=frozenset({normalized}) if normalized else frozenset(),
    )


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


def scope_where_sql(
    column: str, visibility: TenantVisibility, *, placeholder: str = "?"
) -> tuple[str, list[str]]:
    """Render the same rule as a parameter-bound SQL predicate.

    Returns ``(predicate, params)``. The tenant is never interpolated into the
    predicate — it is bound, matching the existing scope push-down. The two
    degenerate profiles render as constants so a caller can decide not to issue
    the query at all:

    - unrestricted -> ``("1=1", [])``
    - empty scope  -> ``("1=0", [])``, matching the existing "empty site scope
      short-circuits and issues no SQL" semantics.

    ``placeholder`` is the driver's parameter marker (``?`` for sqlite3, ``%s``
    for pymysql) and carries no meaning of its own: the visibility rule is the
    predicate and the bound values, which are identical either way.
    """
    if visibility.unrestricted:
        return "1=1", []
    if visibility.empty:
        return "1=0", []
    tenants = sorted(visibility.allowed or ())
    if len(tenants) == 1:
        return f"{column}={placeholder}", [tenants[0]]
    placeholders = ", ".join(placeholder for _ in tenants)
    return f"{column} IN ({placeholders})", list(tenants)
