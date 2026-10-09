"""Runtime registry binding ``(tenant, business entry)`` to one Dify app.

Dify has **no tenant concept**: its only permission boundary is the workspace,
and an app's configuration carries no notion of which of our tenants it serves.
So the tenant/entry decision cannot come from Dify at all — it has to be held
here, explicitly, as data. This module is that registry and nothing else:

* one row is one binding — one tenant, one business entry, the Dify ``app_id``
  the entry is served by, and the name of the agent in our store that app lands
  on;
* a lookup returns a binding or nothing. It never infers: a tenant with no row
  is not "probably the newest agent", it is unmapped;
* once **any** row exists the registry is *configured*, and an unmapped
  ``(tenant, entry)`` selects nothing at all — the fail-closed half. While no
  row exists, the runtime keeps the selection rule it had before this registry
  existed, so an environment that has not adopted it behaves byte-for-byte as
  it did. That is what makes the gate safe to ship inert.

Rows are written by ``aiops admin reconcile`` from the environment manifest
(``[[dify_apps]]``) — the same convergence path that publishes the agents — so
the binding and the agent it names are declared, reviewed and converged
together. The section is the manifest's **complete** desired state for the
registry, so a binding removed from the manifest is removed from the store:
a mapping table that cannot forget a mapping is not a fail-closed registry.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

#: The two business entries the platform rule already defines (``faq.PLATFORMS``).
#: Restated rather than imported so this module stays a leaf: importing ``faq``
#: would drag ``pymysql`` into every runtime that only needs the registry.
BUSINESS_ENTRIES = ("consumer", "operator")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyAppRegistryError(RuntimeError):
    """A binding that cannot be stored as written."""


@dataclass(frozen=True, slots=True)
class DifyAppBinding:
    """One explicit ``(tenant, entry) → Dify app`` mapping.

    ``agent_name`` is how the app lands in our store. It is a name, not an
    ``agent_id``: the id changes on every re-publish, and a registry that had to
    be rewritten on each publish would be one more thing to forget. The name is
    already the convergence key ``agent_manifest`` uses.

    ``app_id`` is the other half, and it is not redundant: it is the identity the
    **pull** side needs (``dify_dsl_pull`` exports one app by id). Keeping both
    on one row is what makes "this tenant's answers come from that Dify app" a
    single reviewable fact instead of a pairing a human re-derives.
    """

    tenant_id: str
    business_entry: str
    app_id: str
    agent_name: str


class DifyAppRegistry:
    """The binding table, sharing the gateway SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dify_app_registry (
                    tenant_id TEXT NOT NULL,
                    business_entry TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, business_entry)
                );
                """
            )
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            if write:
                connection.commit()
        finally:
            connection.close()

    # ── reads ─────────────────────────────────────────────────────────

    def lookup(self, tenant_id: str, business_entry: str) -> DifyAppBinding | None:
        """The binding for exactly this pair, or None. Never a fallback."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM dify_app_registry WHERE tenant_id = ? AND business_entry = ?",
                (tenant_id, business_entry),
            ).fetchone()
        return _binding_from_row(row) if row is not None else None

    def all(self) -> tuple[DifyAppBinding, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM dify_app_registry ORDER BY tenant_id, business_entry"
            ).fetchall()
        return tuple(_binding_from_row(row) for row in rows)

    def is_configured(self) -> bool:
        """Has this environment adopted the registry at all?

        The gate's only switch, and deliberately data-driven: an environment
        whose manifest declares no ``[[dify_apps]]`` has an empty table and
        keeps the selection rule it had before this module existed.
        """
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM dify_app_registry LIMIT 1").fetchone() is not None

    # ── writes ────────────────────────────────────────────────────────

    def put(self, binding: DifyAppBinding) -> None:
        binding = _validated(binding)
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO dify_app_registry (tenant_id, business_entry, app_id, agent_name, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, business_entry) DO UPDATE SET
                    app_id = excluded.app_id,
                    agent_name = excluded.agent_name,
                    updated_at = excluded.updated_at
                """,
                (
                    binding.tenant_id,
                    binding.business_entry,
                    binding.app_id,
                    binding.agent_name,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def delete(self, tenant_id: str, business_entry: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                "DELETE FROM dify_app_registry WHERE tenant_id = ? AND business_entry = ?",
                (tenant_id, business_entry),
            )


def _binding_from_row(row: sqlite3.Row) -> DifyAppBinding:
    return DifyAppBinding(
        tenant_id=str(row["tenant_id"]),
        business_entry=str(row["business_entry"]),
        app_id=str(row["app_id"]),
        agent_name=str(row["agent_name"]),
    )


def _validated(binding: DifyAppBinding) -> DifyAppBinding:
    if not _SAFE_ID.fullmatch(binding.tenant_id):
        raise DifyAppRegistryError(f"tenant_id is invalid: {binding.tenant_id!r}")
    if binding.business_entry not in BUSINESS_ENTRIES:
        raise DifyAppRegistryError(
            f"business_entry must be one of {list(BUSINESS_ENTRIES)}, got {binding.business_entry!r}"
        )
    if not binding.app_id or not _SAFE_ID.fullmatch(binding.app_id):
        raise DifyAppRegistryError(f"app_id is invalid: {binding.app_id!r}")
    if not binding.agent_name.strip():
        raise DifyAppRegistryError("agent_name must be a non-empty name")
    return binding


def validate_bindings(bindings: Iterable[DifyAppBinding]) -> tuple[DifyAppBinding, ...]:
    """Structural preflight: every row valid, no pair declared twice.

    Raises before the caller writes anything, so an invalid manifest leaves the
    store untouched — the same contract ``agent_manifest`` keeps for agents.
    """
    validated: list[DifyAppBinding] = []
    seen: set[tuple[str, str]] = set()
    for index, binding in enumerate(bindings):
        try:
            checked = _validated(binding)
        except DifyAppRegistryError as exc:
            raise DifyAppRegistryError(f"dify_apps[{index}]: {exc}") from exc
        key = (checked.tenant_id, checked.business_entry)
        if key in seen:
            raise DifyAppRegistryError(
                f"dify_apps[{index}]: {checked.tenant_id}/{checked.business_entry} 重复声明"
            )
        seen.add(key)
        validated.append(checked)
    return tuple(validated)
