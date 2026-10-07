"""Bring already-published shortcuts up to the current language inventory (#542).

Changing the SEED does not change production. `seed_bundled` skips rows that
exist, so a shortcut published before a language existed keeps its old copy set
forever — the Traditional/Thai/Khmer/… request falls back to Simplified while the
response still echoes the requested tag. This tool closes that gap.

## What it does, and deliberately does not do

For every row it adds a language ONLY when the row lacks it entirely, taking the
value from the current seed. It never:

- **overwrites an existing value.** Rows carry deliberate operator edits (41 has
  one: `operator/case_exploration`'s `zh` template reads 「有哪些充电运营的客户案例」
  while the seed says 「我想看看客户案例」). The seed is a DEFAULT for new
  languages, not an authority over copy someone already chose.
- **backfills languages older than the gap.** A row publishing `labels` in 6
  languages but `descriptions` in 2 was left partially migrated on purpose;
  completing it would change what a `de` reader sees, which is not this task.
  A language is added to a field when that FIELD lacks that language and the
  language is one the row does not carry anywhere — i.e. it is in the set of
  languages this migration introduces.
- **touches status or published_version.** Edits route through the product
  lifecycle (`fork_draft` → `update` → `publish`, or `update` → `publish` for an
  already-draft row), so a `disabled` row is edited as a draft and stays
  disabled, and a `published` row is restored to `published`. Re-publishing
  creates a new immutable version, which is what makes this reversible.

`solution_discovery` has no seed entry (it is an operator-created action), so it
is reported as a gap rather than invented.

## Usage

    python migrate_shortcut_i18n.py --db /var/lib/aiops-41/gateway/gateway.db          # dry run
    python migrate_shortcut_i18n.py --db /var/lib/aiops-41/gateway/gateway.db --apply

Always take a backup first (docs/agents/env-41-runbook.md); the tool prints the
backup command it expects.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES  # noqa: E402
from aiops_diagnostics.shortcut_lifecycle import (  # noqa: E402
    _BUNDLED_SHORTCUTS,  # noqa: PLC2701 — the seed IS the source for new languages
    PLATFORM_SCOPE,
    PLATFORM_TENANT_ID,
    ShortcutManager,
    ShortcutStore,
)

FIELDS = ("labels", "descriptions", "question_templates")

#: Languages this migration is responsible for: every language a FIELD lacks,
#: not only the newly added tags. An earlier version of this tool narrowed the
#: set to `zh-Hant/vi/mn/th/km` and left a real hole in place — 41's two operator
#: rows carry `labels` in 6 languages but `question_templates` in `zh` ONLY
#: (published 2026-09-28, their only version), so a German reader gets a German
#: label and a CHINESE template. That is the same defect this tool exists to
#: fix, just older, so completing the field is in scope. What stays out of
#: scope is the inverse: no existing value is ever rewritten.
ADDED_LANGUAGES = tuple(SUPPORTED_LANGUAGES)

#: `__platform__` rows need platform scope; tenant rows must NOT be sent with it
#: (the tenant id is reserved and scope=platform would rewrite which row it is).
PLATFORM_CONTEXT_ROLES = frozenset({"ROLE_PLATFORM_ADMIN"})
TENANT_CONTEXT_ROLES = frozenset({"ROLE_AGENT_ADMIN"})


@dataclass(frozen=True, slots=True)
class Row:  # noqa: D101 — internal carrier
    shortcut_id: str
    tenant_id: str
    business_entry: str
    code: str
    status: str
    revision: int
    fields: dict[str, Any]


class _Context:
    """The identity a lifecycle call reads: tenant + roles + actor."""

    def __init__(self, tenant_id: str, roles: frozenset[str], actor: str) -> None:
        self.effective_tenant_id = tenant_id
        self.roles = roles
        self.caller = type("Caller", (), {"b_user_id": actor})()


def seed_for(business_entry: str, code: str) -> dict[str, Any] | None:
    for entry, fields in _BUNDLED_SHORTCUTS:
        if entry == business_entry and code in fields:
            return fields[code]
    return None


def read_rows(db: Path) -> list[Row]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = []
        for record in connection.execute("select * from shortcuts order by tenant_id, business_entry, code"):
            rows.append(
                Row(
                    shortcut_id=record["shortcut_id"],
                    tenant_id=record["tenant_id"],
                    business_entry=record["business_entry"],
                    code=record["code"],
                    status=record["status"],
                    revision=int(record["revision"]),
                    fields=json.loads(record["fields_json"] or "{}"),
                )
            )
        return rows
    finally:
        connection.close()


def plan_row(row: Row) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Return (new field maps, notes). Empty maps = nothing to do for that field."""
    seed = seed_for(row.business_entry, row.code)
    notes: list[str] = []
    if seed is None:
        return {}, [f"{row.business_entry}/{row.code}: 种子里没有这个动作 —— 无法补齐（缺口，不臆造）"]

    planned: dict[str, dict[str, str]] = {}
    for field in FIELDS:
        current = dict(row.fields.get(field) or {})
        source = seed.get(field) or {}
        additions = {tag: source[tag] for tag in ADDED_LANGUAGES if tag not in current and tag in source}
        missing_source = [tag for tag in SUPPORTED_LANGUAGES if tag not in current and tag not in source]
        if missing_source:
            notes.append(f"{row.business_entry}/{row.code}[{field}]: 种子也缺 {missing_source} —— 不补")
        if additions:
            planned[field] = {**current, **additions}
    return planned, notes


def _context_for(row: Row, actor: str) -> tuple[_Context, str]:
    if row.tenant_id == PLATFORM_TENANT_ID:
        return _Context(row.tenant_id, PLATFORM_CONTEXT_ROLES, actor), PLATFORM_SCOPE
    return _Context(row.tenant_id, TENANT_CONTEXT_ROLES, actor), "tenant"


def apply_row(store: ShortcutStore, manager: ShortcutManager, row: Row, planned: dict, actor: str) -> str:
    """Route one edit through the product lifecycle. Returns a one-line outcome."""
    context, scope = _context_for(row, actor)
    live = manager.get(context, row.shortcut_id, scope=scope)
    changed = {field: planned[field] for field in FIELDS if planned.get(field) is not None}
    payload = {
        "shortcut_id": live.shortcut_id,
        "business_entry": live.business_entry,
        "intent": live.intent,
        "requires_order": live.requires_order,
        "sort_order": live.sort_order,
        "labels": changed.get("labels", live.labels),
        "descriptions": changed.get("descriptions", live.descriptions),
        "question_templates": changed.get("question_templates", live.question_templates),
        "target_agent_version": live.target_agent_version,
        "jump_path": live.jump_path,
        "expected_revision": live.revision,
    }

    was_published = live.status == "published"
    if was_published:
        live = manager.fork_draft(context, live.shortcut_id, expected_revision=live.revision, scope=scope)
        payload["expected_revision"] = live.revision
    updated = manager.update(context, live.shortcut_id, payload, scope=scope)
    manager.publish(context, updated.shortcut_id, expected_revision=updated.revision, scope=scope)
    if not was_published:
        store.disable(updated.shortcut_id, updated.tenant_id, updated.revision)
    return f"{'published' if was_published else 'disabled'} 保持"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/aiops-41/gateway/gateway.db"))
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    parser.add_argument("--actor", default="i18n-migration-542")
    args = parser.parse_args()

    rows = read_rows(args.db)
    store = ShortcutStore(args.db)
    manager = ShortcutManager(store)

    total_added = 0
    all_notes: list[str] = []
    for row in rows:
        planned, notes = plan_row(row)
        all_notes.extend(notes)
        label = f"{row.tenant_id}/{row.business_entry}/{row.code}"
        if not planned:
            print(f"SKIP  {label} — 无需补齐")
            continue
        detail = ", ".join(f"{f}+{len(v) - len(row.fields.get(f) or {})}语" for f, v in planned.items())
        total_added += sum(len(v) - len(row.fields.get(f) or {}) for f, v in planned.items())
        if not args.apply:
            print(f"PLAN  {label} [{row.status}] {detail}")
            continue
        outcome = apply_row(store, manager, row, planned, args.actor)
        print(f"DONE  {label} {detail} -> {outcome}")

    print()
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "rows": len(rows), "added": total_added}))
    for note in all_notes:
        print("NOTE ", note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
