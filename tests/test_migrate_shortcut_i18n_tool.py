"""The shortcut i18n migration tool (#542).

Review found four defects in the first version, all in how it treated row STATE.
This suite pins the behaviour per state — published, draft, disabled — against a
real temporary database, because the defects were state-specific and a single
"it migrated something" assertion would have passed while three of them were live.

`opencc` is not a dependency here (see the tool's sibling in the repo for why),
so these tests need none: the tool copies values out of the seed.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

ROOT = Path(__file__).parents[1]
SIX = ("zh", "en", "de", "fr", "es", "pt")


def _load():
    import sys

    spec = importlib.util.spec_from_file_location(
        "migrate_shortcut_i18n", ROOT / "tools" / "migrate_shortcut_i18n.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered BEFORE exec: the module defines a dataclass, and dataclasses
    # resolves the defining module through sys.modules to build slots.
    sys.modules["migrate_shortcut_i18n"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("migrate_shortcut_i18n", None)
    return module


@pytest.fixture()
def tool():
    return _load()


def _digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ctx(tenant: str = "T-1", roles: frozenset[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_tenant_id=tenant,
        roles=roles or frozenset({"ROLE_AGENT_ADMIN"}),
        caller=SimpleNamespace(b_user_id="admin"),
    )


def _six_lang(text: str) -> dict[str, str]:
    return {tag: f"{text}-{tag}" for tag in SIX}


def _row(store: ShortcutStore, *, status: str, tenant: str = "T-1", code: str = "case_exploration"):
    """Create a row in the requested state, then force that state directly.

    The public API can only reach `published` (via publish) and `draft`; `disabled`
    needs a disable call. Doing it through the API keeps the schema honest.
    """
    context = _ctx(tenant)
    row = store.create(
        tenant,
        "consumer",
        code,
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels=_six_lang("label"),
        descriptions=_six_lang("desc"),
        question_templates=_six_lang("tmpl"),
        target_agent_version=None,
        created_by="test",
    )
    manager = ShortcutManager(store)
    if status == "published":
        manager.publish(context, row.shortcut_id, expected_revision=row.revision)
    elif status == "disabled":
        manager.publish(context, row.shortcut_id, expected_revision=row.revision)
        live = store.get(row.shortcut_id, tenant)
        store.disable(live.shortcut_id, tenant, live.revision)
    return store.get(row.shortcut_id, tenant)


def _fields(store: ShortcutStore, shortcut_id: str, tenant: str = "T-1") -> dict:
    live = store.get(shortcut_id, tenant)
    return {
        "labels": live.labels,
        "descriptions": live.descriptions,
        "question_templates": live.question_templates,
    }


def _record(store: ShortcutStore, shortcut_id: str, tenant: str = "T-1") -> dict:
    """The raw sqlite row the tool's preview path reads."""
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return dict(
            connection.execute("select * from shortcuts where shortcut_id = ?", (shortcut_id,)).fetchone()
        )
    finally:
        connection.close()


def test_published_row_is_completed_and_stays_published(tmp_path: Path, tool) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    row = _row(store, status="published")
    manager = ShortcutManager(store)

    outcome = tool.migrate_row(store, manager, _record(store, row.shortcut_id), "test")

    assert "published 保持" in outcome
    after = store.get(row.shortcut_id, "T-1")
    assert after.status == "published"
    for field in ("labels", "descriptions", "question_templates"):
        assert set(getattr(after, field)) == set(SUPPORTED_LANGUAGES), f"{field} 未补齐"
    # The public listing shows it, and it now serves a language it previously fell back on.
    listed = store.list_published("T-1", "consumer")
    assert listed[0].public("zh-Hant")["language"] == "zh-Hant"


def test_draft_stays_a_draft_and_is_never_published(tmp_path: Path, tool) -> None:
    """A draft is unapproved; publishing it would expose it to users.

    This is the defect review caught: the first version published drafts and then
    failed to re-disable them, leaving an unapproved action in the public listing.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    row = _row(store, status="draft")
    manager = ShortcutManager(store)

    outcome = tool.migrate_row(store, manager, _record(store, row.shortcut_id), "test")

    assert "draft 保持" in outcome
    after = store.get(row.shortcut_id, "T-1")
    assert after.status == "draft", "草稿被发布了"
    assert set(after.labels) == set(SUPPORTED_LANGUAGES), "草稿未被补齐"
    assert store.list_published("T-1", "consumer") == [], "草稿出现在了公开列表里"


def test_disabled_row_is_skipped_and_reported(tmp_path: Path, tool) -> None:
    """A disabled row is not enabled to edit it — a crash mid-cycle would expose it.

    Recorded as skipped rather than silently left at six languages.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    row = _row(store, status="disabled")
    manager = ShortcutManager(store)
    before = _fields(store, row.shortcut_id)

    outcome = tool.migrate_row(store, manager, _record(store, row.shortcut_id), "test")

    assert "SKIP" in outcome and "disabled" in outcome
    after = store.get(row.shortcut_id, "T-1")
    assert after.status == "disabled", "停用行被改动了状态"
    assert _fields(store, row.shortcut_id) == before, "停用行被改动了文案"


def test_existing_values_are_never_rewritten(tmp_path: Path, tool) -> None:
    """The operator's own copy is preserved — the seed only fills absences."""
    store = ShortcutStore(tmp_path / "gateway.db")
    row = _row(store, status="published")
    # An operator edit: a value the seed does NOT contain.
    manager = ShortcutManager(store)
    live = store.get(row.shortcut_id, "T-1")
    manager.fork_draft(_ctx(), live.shortcut_id, expected_revision=live.revision)
    draft = store.get(row.shortcut_id, "T-1")
    manager.update(
        _ctx(),
        draft.shortcut_id,
        {
            "business_entry": "consumer",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {**draft.labels, "zh": "运营自己写的标签"},
            "descriptions": draft.descriptions,
            "question_templates": draft.question_templates,
            "target_agent_version": None,
            "jump_path": None,
            "expected_revision": draft.revision,
        },
    )
    edited = store.get(row.shortcut_id, "T-1")
    manager.publish(_ctx(), edited.shortcut_id, expected_revision=edited.revision)

    tool.migrate_row(store, manager, _record(store, row.shortcut_id), "test")

    after = store.get(row.shortcut_id, "T-1")
    assert after.labels["zh"] == "运营自己写的标签", "运营自己的文案被种子覆盖了"
    assert set(after.labels) == set(SUPPORTED_LANGUAGES)


def test_row_without_a_seed_is_reported_not_invented(tmp_path: Path, tool) -> None:
    """`solution_discovery` has no seed — reported as a gap, never fabricated."""
    store = ShortcutStore(tmp_path / "gateway.db")
    context = _ctx()
    row = store.create(
        "T-1",
        "consumer",
        "solution_discovery",
        intent="solution_discovery",
        requires_order=False,
        sort_order=30,
        labels={"zh": "行业方案", "en": "Industry Solutions"},
        descriptions={"zh": "看方案", "en": "See solutions"},
        question_templates={"zh": "有哪些方案", "en": "Which solutions"},
        target_agent_version=None,
        created_by="operator",
    )
    manager = ShortcutManager(store)
    manager.publish(context, row.shortcut_id, expected_revision=row.revision)

    outcome = tool.migrate_row(store, manager, _record(store, row.shortcut_id), "test")

    assert "SKIP" in outcome and "种子里没有" in outcome
    assert set(store.get(row.shortcut_id, "T-1").labels) == {"zh", "en"}, "臆造了文案"


def test_a_concurrent_edit_survives_a_stale_snapshot(tmp_path: Path, tool) -> None:
    """An operator edit between the read and the write must not be overwritten.

    This is the defect as it actually occurred: the tool read every row up front,
    planned from that snapshot, and later wrote the snapshot's field map back
    under the row's CURRENT revision — so the revision check passed and the
    operator's newer text was replaced by the older one.

    Exercised through `migrate_row` with a deliberately STALE record, which is
    what the caller holds after the real edit happens. Asserting `plan()` alone
    would not catch it: that is not the seam the bug was in.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    row = _row(store, status="published")
    manager = ShortcutManager(store)
    stale_record = _record(store, row.shortcut_id)  # snapshot taken BEFORE the edit

    # The operator edits `en` — a language already present, so it must survive.
    live = store.get(row.shortcut_id, "T-1")
    manager.fork_draft(_ctx(), live.shortcut_id, expected_revision=live.revision)
    draft = store.get(row.shortcut_id, "T-1")
    manager.update(
        _ctx(),
        draft.shortcut_id,
        {
            "business_entry": "consumer",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {**draft.labels, "en": "OPERATOR-EDIT"},
            "descriptions": draft.descriptions,
            "question_templates": draft.question_templates,
            "target_agent_version": None,
            "jump_path": None,
            "expected_revision": draft.revision,
        },
    )
    edited = store.get(row.shortcut_id, "T-1")
    manager.publish(_ctx(), edited.shortcut_id, expected_revision=edited.revision)

    tool.migrate_row(store, manager, stale_record, "test")

    after = store.get(row.shortcut_id, "T-1")
    assert after.labels["en"] == "OPERATOR-EDIT", "用陈旧快照覆盖了运营的新文案"
    assert set(after.labels) == set(SUPPORTED_LANGUAGES), "没有完成补齐"


def test_plan_is_computed_against_live_values_not_a_snapshot(tool) -> None:
    """Planning from a startup snapshot is what silently overwrote an operator edit."""
    seed = {"labels": {tag: f"seed-{tag}" for tag in SUPPORTED_LANGUAGES}}
    # The live row already has `en` changed by an operator AFTER the snapshot was
    # taken; the plan must add the missing tags and leave `en` alone.
    live = {"labels": {"zh": "x", "en": "operator-edited"}}
    plans, _ = tool.plan(seed, live)

    additions = {item.field: item.additions for item in plans}
    assert "en" not in additions["labels"], "把运营改过的值当成缺失项了"
    assert "zh-Hant" in additions["labels"]


def test_preview_does_not_touch_the_database(tmp_path: Path, tool) -> None:
    """A preview must not write (caught in review: the store's __init__ writes).

    Asserts the DATABASE CONTENT is byte-identical rather than the directory
    listing: the file is in WAL mode, and any reader of a WAL database attaches a
    `-shm` sidecar — that is sqlite, not the tool. What matters is that the
    preview neither changes a row nor creates the database when it is absent.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    _row(store, status="published")
    before = _digest(store.path)

    tool.preview(store.path)

    assert _digest(store.path) == before, "预览改动了数据库内容"

    # And a preview of a NON-EXISTENT path must fail loudly without creating one.
    missing = tmp_path / "absent.db"
    with pytest.raises(SystemExit):
        tool.preview(missing)
    assert not missing.exists(), "预览凭空建了一个数据库"
