"""Idempotent migration from tenant-copied shortcuts to platform defaults (#247)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.shortcut_lifecycle import (
    PLATFORM_SCOPE,
    PLATFORM_TENANT_ID,
    Shortcut,
    ShortcutManager,
)


@dataclass(frozen=True, slots=True)
class ShortcutMigrationReport:
    business_entry: str
    code: str
    action: str
    source_tenant_id: str | None = None
    platform_shortcut_id: str | None = None


def migrate_shortcuts(
    manager: ShortcutManager,
    context: Any,
    *,
    dry_run: bool = False,
) -> list[ShortcutMigrationReport]:
    """Create one platform default per published tenant code, without deletion.

    The first unbound published row is used as copy source when possible. A
    tenant-specific ``target_agent_version`` is deliberately removed from the
    platform snapshot; the original tenant row remains the local binding.
    """
    store = manager.store
    reports: list[ShortcutMigrationReport] = []
    for entry in ("consumer", "operator"):
        grouped: dict[str, list[Shortcut]] = {}
        for row in store.list_published_for_entry(entry):
            grouped.setdefault(row.code, []).append(row)
        for code, rows in sorted(grouped.items()):
            existing = store.find_by_code(PLATFORM_TENANT_ID, entry, code)
            if existing is not None:
                reports.append(
                    ShortcutMigrationReport(
                        entry,
                        code,
                        f"unchanged:{existing.status}",
                        platform_shortcut_id=existing.shortcut_id,
                    )
                )
                continue
            source = min(
                rows,
                key=lambda row: (row.target_agent_version is not None, row.created_at, row.tenant_id),
            )
            if dry_run:
                reports.append(ShortcutMigrationReport(entry, code, "create", source.tenant_id))
                continue
            created = manager.create(
                context,
                {
                    "business_entry": entry,
                    "code": code,
                    "intent": source.intent,
                    "requires_order": source.requires_order,
                    "sort_order": source.sort_order,
                    "labels": source.labels,
                    "descriptions": source.descriptions,
                    "question_templates": source.question_templates,
                    "target_agent_version": None,
                    # A tenant row shadows the platform default in the
                    # effective merge, so a platform default that dropped this
                    # would be silently ineffective for every tenant that
                    # already has a copy.
                    "jump_path": source.jump_path,
                },
                scope=PLATFORM_SCOPE,
            )
            try:
                manager.publish(
                    context, created.shortcut_id, expected_revision=created.revision, scope=PLATFORM_SCOPE
                )
            except Exception:
                # Keep a failed migration from leaving an unpublished platform draft.
                manager.delete(
                    context,
                    created.shortcut_id,
                    expected_revision=created.revision,
                    scope=PLATFORM_SCOPE,
                )
                raise
            reports.append(
                ShortcutMigrationReport(entry, code, "created", source.tenant_id, created.shortcut_id)
            )
    return reports
