"""Bring already-published shortcuts up to the current language inventory (#542).

Changing the SEED does not change production. `seed_bundled` skips rows that
exist, so a shortcut published before a language existed keeps its old copy set
forever — a Traditional/Thai/Khmer/… request falls back to Simplified while the
response still echoes the requested tag. This tool closes that gap.

## What it does

For every row, and for each of `labels` / `descriptions` / `question_templates`,
it adds **every language in the inventory that the field is missing**, taking the
value from the current seed.

## What it deliberately does NOT do

- **Existing values are never rewritten.** Rows carry operator edits (41 has two:
  a tenant's `labels.en='Solution Discovery'` and an operator's `zh` template
  「有哪些充电运营的客户案例」). The seed is a default for MISSING languages only,
  never an authority over copy someone already chose.
- **A draft is never published.** Drafts are edited in place and stay drafts.
  Publishing an unapproved action to make it translatable would be far worse than
  the translation gap being fixed.
- **A disabled row is skipped and reported.** Editing one requires an
  enable→edit→disable cycle, and a crash inside that cycle leaves the row ENABLED
  and user-visible. A disabled row serves nobody, so it has no language gap to
  close; the report keeps it visible instead of leaving it silently at 6 languages.
- **Rows with no seed entry are reported, not invented.** `solution_discovery` is
  an operator-created action with no seed.

## Concurrency

Plan and write both run against a row read IMMEDIATELY before the write, and the
payload carries that read's revision. An operator edit landing in between
collides with the revision check, the row is re-read and re-planned, and the
edit is preserved. Planning from a snapshot taken at startup is what silently
overwrote a concurrent edit (caught in review).

## Usage

    python migrate_shortcut_i18n.py --db /var/lib/aiops-41/gateway/gateway.db          # preview (no write)
    python migrate_shortcut_i18n.py --db /var/lib/aiops-41/gateway/gateway.db --apply

Take a backup first, and prefer per-shortcut `rollback` for recovery over
restoring the whole database — the database also holds jobs and tenants, so a
whole-file restore discards every write made after the backup. See the runbook.
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
    _BUNDLED_SHORTCUTS,  # noqa: PLC2701 — the seed IS the source for missing languages
    PLATFORM_SCOPE,
    PLATFORM_TENANT_ID,
    ShortcutConflict,
    ShortcutManager,
    ShortcutStore,
)

FIELDS = ("labels", "descriptions", "question_templates")
PLATFORM_CONTEXT_ROLES = frozenset({"ROLE_PLATFORM_ADMIN"})
TENANT_CONTEXT_ROLES = frozenset({"ROLE_AGENT_ADMIN"})
MAX_ATTEMPTS = 5

#: Statuses this tool will edit. `disabled` is excluded on purpose — see the
#: module docstring; it is reported instead.
EDITABLE_STATUSES = ("published", "draft")


@dataclass(frozen=True, slots=True)
class Plan:  # noqa: D101 — internal carrier
    field: str
    additions: dict[str, str]

    @property
    def count(self) -> int:
        return len(self.additions)


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


def _context_for(tenant_id: str, actor: str) -> tuple[_Context, str]:
    if tenant_id == PLATFORM_TENANT_ID:
        return _Context(tenant_id, PLATFORM_CONTEXT_ROLES, actor), PLATFORM_SCOPE
    return _Context(tenant_id, TENANT_CONTEXT_ROLES, actor), "tenant"


def plan(seed: dict[str, Any], fields: dict[str, Any]) -> tuple[list[Plan], list[str]]:
    """Additions per field, computed against the CURRENT field maps.

    `fields` must be the row's live values — planning from a startup snapshot is
    what let a concurrent operator edit be overwritten (caught in review).
    """
    plans: list[Plan] = []
    notes: list[str] = []
    for field in FIELDS:
        current = fields.get(field) or {}
        source = seed.get(field) or {}
        additions = {tag: source[tag] for tag in SUPPORTED_LANGUAGES if tag not in current and tag in source}
        missing_source = [tag for tag in SUPPORTED_LANGUAGES if tag not in current and tag not in source]
        if missing_source:
            notes.append(f"{field} 种子也缺 {missing_source}")
        if additions:
            plans.append(Plan(field=field, additions=additions))
    return plans, notes


def migrate_row(store: ShortcutStore, manager: ShortcutManager, record: Any, actor: str) -> str:
    """Migrate one row, preserving its status exactly. Returns a one-line outcome."""
    context, scope = _context_for(record["tenant_id"], actor)
    seed = seed_for(record["business_entry"], record["code"])
    if seed is None:
        return "SKIP 种子里没有这个动作（缺口，不臆造）"

    for _attempt in range(MAX_ATTEMPTS):
        live = manager.get(context, record["shortcut_id"], scope=scope)
        if live.status not in EDITABLE_STATUSES:
            return f"SKIP {live.status} —— 本工具不改（改它要 enable→disable，风险大于收益）"
        plans, notes = plan(seed, {field: getattr(live, field) for field in FIELDS})
        if not plans:
            return "SKIP 无需补齐"
        payload = {
            "business_entry": live.business_entry,
            "intent": live.intent,
            "requires_order": live.requires_order,
            "sort_order": live.sort_order,
            "labels": live.labels,
            "descriptions": live.descriptions,
            "question_templates": live.question_templates,
            "target_agent_version": live.target_agent_version,
            "jump_path": live.jump_path,
            # Not copy, and not editable (`kind`). Carried through for the same
            # reason `jump_path` already was: this tool's job is filling in
            # languages, and an update that dropped these would turn a language
            # backfill into a change of what the row IS. `image_url` especially —
            # the request model defaults it to None, so omitting it would
            # silently remove an operator's banner image.
            "image_url": live.image_url,
            "expected_revision": live.revision,
        }
        for item in plans:
            payload[item.field] = {**(getattr(live, item.field) or {}), **item.additions}
        try:
            if live.status == "published":
                # Published rows are edited through the lifecycle, which creates a
                # NEW immutable version — that version is the rollback point.
                draft = manager.fork_draft(
                    context, live.shortcut_id, expected_revision=live.revision, scope=scope
                )
                payload["expected_revision"] = draft.revision
                updated = manager.update(context, live.shortcut_id, payload, scope=scope)
                manager.publish(context, updated.shortcut_id, expected_revision=updated.revision, scope=scope)
            else:
                # A draft is edited AS A DRAFT and never published.
                manager.update(context, live.shortcut_id, payload, scope=scope)
        except ShortcutConflict:
            continue  # an operator edited it between our read and write; re-read
        detail = ", ".join(f"{item.field}+{item.count}语" for item in plans)
        return f"{detail} -> {live.status} 保持" + (f"；{'；'.join(notes)}" if notes else "")
    return "FAIL 连续冲突，已放弃该行（未改动）"


def _read_rows(db: Path) -> list[sqlite3.Row]:
    # A preview reads through plain sqlite and never constructs the store, so it
    # cannot create or migrate schema, journal files, or locks (caught in review:
    # the store's __init__ writes even for a dry run).
    if not db.exists():
        raise SystemExit(f"找不到网关数据库：{db}")
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute("select * from shortcuts order by tenant_id, business_entry, code"))
    finally:
        connection.close()


def preview(db: Path) -> int:
    rows = _read_rows(db)
    totals = 0
    for record in rows:
        label = f"{record['tenant_id']}/{record['business_entry']}/{record['code']}"
        seed = seed_for(record["business_entry"], record["code"])
        if seed is None:
            print(f"SKIP  {label} — 种子里没有这个动作（缺口，不臆造）")
            continue
        if record["status"] not in EDITABLE_STATUSES:
            print(f"SKIP  {label} [{record['status']}] — 本工具不改")
            continue
        plans, _ = plan(seed, json.loads(record["fields_json"] or "{}"))
        if not plans:
            print(f"SKIP  {label} [{record['status']}] — 无需补齐")
            continue
        totals += sum(item.count for item in plans)
        detail = ", ".join(f"{item.field}+{item.count}语" for item in plans)
        print(f"PLAN  {label} [{record['status']}] {detail}")
    print()
    print(json.dumps({"mode": "preview", "rows": len(rows), "added": totals}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/aiops-41/gateway/gateway.db"))
    parser.add_argument("--apply", action="store_true", help="write; without it this is a preview")
    parser.add_argument("--actor", default="i18n-migration-542")
    args = parser.parse_args()

    if not args.apply:
        return preview(args.db)

    rows = _read_rows(args.db)
    store = ShortcutStore(args.db)
    manager = ShortcutManager(store)
    failed = 0
    for record in rows:
        label = f"{record['tenant_id']}/{record['business_entry']}/{record['code']}"
        try:
            print(f"DONE  {label} {migrate_row(store, manager, record, args.actor)}")
        except Exception as exc:  # noqa: BLE001 — one row must not abort the rest
            failed += 1
            print(f"FAIL  {label} — {type(exc).__name__}: {exc}", file=sys.stderr)
    print()
    print(json.dumps({"mode": "apply", "rows": len(rows), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
