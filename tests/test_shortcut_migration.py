from __future__ import annotations

from pathlib import Path

import pytest

from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore
from aiops_diagnostics.shortcut_migration import migrate_shortcuts


def _context() -> ScopeContext:
    subject = SubjectRecord(b_user_id="migration", tenant_id="platform")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="platform",
        data_scope=DataScope(type="all"),
        roles=frozenset({"ROLE_PLATFORM_ADMIN"}),
        permissions=frozenset({"aiops:shortcuts:manage"}),
    )


def _create_published(
    store: ShortcutStore, manager: ShortcutManager, tenant: str, *, target: str | None = None
):
    row = store.create(
        tenant,
        "consumer",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "案例"},
        descriptions={"zh": "说明"},
        question_templates={"zh": "看看案例"},
        target_agent_version=target,
        created_by=tenant,
    )
    tenant_context = type("Ctx", (), {"effective_tenant_id": tenant, "roles": {"ROLE_AGENT_ADMIN"}})()
    manager.publish(tenant_context, row.shortcut_id, expected_revision=row.revision)


def test_migration_creates_platform_default_and_keeps_tenant_binding(tmp_path: Path) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    _create_published(store, manager, "tenant-a", target="agt_12345678#v1")
    _create_published(store, manager, "tenant-b")

    reports = migrate_shortcuts(manager, _context())
    assert [(report.code, report.action) for report in reports] == [("case_exploration", "created")]
    platform = store.find_by_code("__platform__", "consumer", "case_exploration")
    assert platform is not None and platform.status == "published"
    assert platform.target_agent_version is None
    tenant = store.find_by_code("tenant-a", "consumer", "case_exploration")
    assert tenant is not None and tenant.target_agent_version == "agt_12345678#v1"


def test_migration_dry_run_and_repeat_are_idempotent(tmp_path: Path) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    _create_published(store, manager, "tenant-a")

    dry = migrate_shortcuts(manager, _context(), dry_run=True)
    assert [report.action for report in dry] == ["create"]
    assert store.find_by_code("__platform__", "consumer", "case_exploration") is None

    first = migrate_shortcuts(manager, _context())
    second = migrate_shortcuts(manager, _context())
    assert [report.action for report in first] == ["created"]
    assert [report.action for report in second] == ["unchanged:published"]
    assert len(store.list_published("__platform__", "consumer")) == 1


def test_publish_failure_removes_platform_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    _create_published(store, manager, "tenant-a")

    def fail_publish(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(manager, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        migrate_shortcuts(manager, _context())
    assert store.find_by_code("__platform__", "consumer", "case_exploration") is None
    assert store.find_by_code("tenant-a", "consumer", "case_exploration") is not None
