"""The chat-page banner row: its own surface, in the shortcut table (#588).

The banner is NOT a second resource type. It is a row in `shortcuts` with the
same draft/publish/disable lifecycle, the same tenant+platform scope merge, the
same version snapshots and the same eleven-language copy. What makes it a
banner is one explicit column, `kind`, and that is the whole point of these
tests: the surface must be decided by a field that names it, never by which
other fields happen to be set.

Two failure modes this file exists to catch:

* **A banner leaking into the button listing.** The button listing's row count
  is a contract the client renders; one extra row is a stray card where a
  button should be. Pinned here by count AND by code list.
* **The surface being derived from `image_url`.** A row whose `image_url` is
  null must still be a banner (the personalized photo comes from the client),
  and a button must never carry an `image_url` at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiops_diagnostics.shortcut_lifecycle import (
    BANNER_KIND,
    BUTTON_KIND,
    ShortcutManager,
    ShortcutStore,
    ShortcutValidationError,
)

_CONSUMER_BUTTON_CODES = ["case_exploration", "smart_diagnosis", "report_fault"]


class _Ctx:
    effective_tenant_id = "T-1"
    roles = frozenset({"ROLE_AGENT_ADMIN"})
    caller = type("C", (), {"b_user_id": "seed-user"})()


def _seeded(tmp_path: Path) -> tuple[ShortcutStore, ShortcutManager]:
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    for row in store.seed_bundled(_Ctx(), manager):  # type: ignore[arg-type]
        manager.publish(_Ctx(), row.shortcut_id, expected_revision=row.revision)  # type: ignore[arg-type]
    return store, manager


def test_the_banner_row_is_seeded_on_the_consumer_entry_only(tmp_path: Path) -> None:
    """A banner is a client-surface card; the butler entry has no chat page."""
    store, _ = _seeded(tmp_path)
    consumer = store.list_effective("T-1", "consumer")
    operator = store.list_effective("T-1", "operator")

    banners = [row for row in consumer if row.kind == BANNER_KIND]
    assert [row.code for row in banners] == ["battery_report"]
    assert [row for row in operator if row.kind == BANNER_KIND] == []


def test_the_button_listing_is_byte_for_byte_unchanged(tmp_path: Path) -> None:
    """#588 acceptance: the banner must not add a row to /v1/shortcuts.

    Asserted on the codes rather than the rendered JSON: the shape is already
    pinned elsewhere (`public()` is asserted key-by-key), so what this guards
    is the row set — the part a new row would break.
    """
    store, _ = _seeded(tmp_path)
    buttons = store.list_effective("T-1", "consumer", kind=BUTTON_KIND)
    assert [row.code for row in buttons] == _CONSUMER_BUTTON_CODES

    banners = store.list_effective("T-1", "consumer", kind=BANNER_KIND)
    assert [row.code for row in banners] == ["battery_report"]

    # Both surfaces together are the whole effective set — nothing is dropped
    # by the split, and nothing appears on both.
    everything = store.list_effective("T-1", "consumer")
    assert {row.code for row in buttons} | {row.code for row in banners} == {row.code for row in everything}
    assert not {row.code for row in buttons} & {row.code for row in banners}


def test_a_banner_without_an_image_is_still_a_banner(tmp_path: Path) -> None:
    """The surface is `kind`, not "image_url is set".

    The seeded banner ships with no fallback image: the personalized photo is
    the client's own car lookup. If the surface were derived from `image_url`,
    this row would be served as a button — the exact silent mis-serve the
    explicit column exists to prevent.
    """
    store, _ = _seeded(tmp_path)
    banner = store.find_by_code("T-1", "consumer", "battery_report")
    assert banner is not None
    assert banner.kind == BANNER_KIND
    assert banner.image_url is None
    # ...and it is a jump action, on the report page the product names.
    assert banner.jump_path == "/aiPackage/pages/batteryReport/batteryReport"


def test_the_banner_is_a_jump_action_with_no_preset_prompt(tmp_path: Path) -> None:
    """A jump action never reaches the assistant entry, so a preset prompt
    would be dead copy — and the coverage gate would then demand it in eleven
    languages."""
    store, _ = _seeded(tmp_path)
    banner = store.find_by_code("T-1", "consumer", "battery_report")
    assert banner is not None
    assert banner.question_templates == {}
    assert banner.target_agent_version is None


def test_the_button_projection_is_unchanged_and_the_banner_has_its_own(
    tmp_path: Path,
) -> None:
    """Two projections, each carrying only what its surface uses.

    The banner's shape must NOT carry `question_template` or
    `target_agent_version`: a client that saw them would be invited to use a
    field that is meaningless for a card.
    """
    store, _ = _seeded(tmp_path)
    banner = store.find_by_code("T-1", "consumer", "battery_report")
    button = store.find_by_code("T-1", "consumer", "report_fault")
    assert banner is not None and button is not None

    assert button.public("zh")["code"] == "report_fault"
    assert "kind" not in button.public("zh")
    assert "image_url" not in button.public("zh")

    card = banner.banner("zh")
    assert card["code"] == "battery_report"
    assert card["jump_path"] == "/aiPackage/pages/batteryReport/batteryReport"
    assert card["image_url"] is None
    assert set(card) == {"code", "language", "label", "description", "jump_path", "image_url"}


@pytest.mark.parametrize("language", ["zh", "zh-Hant", "en", "de", "fr", "es", "pt", "vi", "mn", "th", "km"])
def test_the_banner_copy_covers_every_language(tmp_path: Path, language: str) -> None:
    """Same copy system as the buttons: eleven languages, no zh fallback.

    A partial banner is the same defect as a partial button — the response
    echoes the requested language while the text is Chinese, which raises
    nothing and is only visible to the user reading it.
    """
    store, _ = _seeded(tmp_path)
    banner = store.find_by_code("T-1", "consumer", "battery_report")
    assert banner is not None
    card = banner.banner(language)
    assert card["language"] == language
    assert card["label"].strip()
    assert card["description"].strip()
    assert banner.missing_translations(language) == []


def test_the_publish_snapshot_freezes_the_kind_and_the_image(tmp_path: Path) -> None:
    """A published version is an immutable snapshot of what was served.

    `kind` and `image_url` are part of what a client renders, so a rollback to
    an older version has to restore them too — otherwise "roll back" would
    restore the copy and quietly keep the current image.
    """
    store, manager = _seeded(tmp_path)
    banner = store.find_by_code("T-1", "consumer", "battery_report")
    assert banner is not None
    # Move it to a draft, set a fallback image, republish.
    manager.fork_draft(_Ctx(), banner.shortcut_id, expected_revision=banner.revision)  # type: ignore[arg-type]
    draft = store.get(banner.shortcut_id, "T-1")
    manager.update(
        _Ctx(),  # type: ignore[arg-type]
        banner.shortcut_id,
        {
            "intent": draft.intent,
            "requires_order": draft.requires_order,
            "sort_order": draft.sort_order,
            "labels": draft.labels,
            "descriptions": draft.descriptions,
            "question_templates": {},
            "target_agent_version": None,
            "jump_path": draft.jump_path,
            "image_url": "https://car3.autoimg.cn/cardfs/series/example.png",
            "expected_revision": draft.revision,
        },
    )
    draft = store.get(banner.shortcut_id, "T-1")
    manager.publish(_Ctx(), banner.shortcut_id, expected_revision=draft.revision)  # type: ignore[arg-type]

    latest = store.version(banner.shortcut_id, "T-1", 2)
    assert latest.snapshot["kind"] == BANNER_KIND
    assert latest.snapshot["image_url"] == "https://car3.autoimg.cn/cardfs/series/example.png"
    first = store.version(banner.shortcut_id, "T-1", 1)
    assert first.snapshot["image_url"] is None


def test_a_button_may_not_carry_an_image(tmp_path: Path) -> None:
    """Two discriminators is worse than one: a button with an image would be
    served as a button and rendered as a card by a client still using the
    image rule. Rejected at configuration time instead."""
    store, manager = _seeded(tmp_path)
    with pytest.raises(ShortcutValidationError, match="image_url is only allowed for a banner"):
        manager.create(
            _Ctx(),  # type: ignore[arg-type]
            {
                "business_entry": "consumer",
                "code": "sneaky",
                "intent": "knowledge",
                "requires_order": False,
                "sort_order": 90,
                "labels": {"zh": "标签"},
                "image_url": "https://example.com/a.png",
            },
        )


def test_a_banner_must_point_somewhere(tmp_path: Path) -> None:
    """A card that navigates nowhere is not a banner; it is a dead card that
    only misconfiguration could create."""
    store, manager = _seeded(tmp_path)
    with pytest.raises(ShortcutValidationError, match="a banner must set jump_path"):
        manager.create(
            _Ctx(),  # type: ignore[arg-type]
            {
                "business_entry": "consumer",
                "code": "dead_card",
                "intent": "knowledge",
                "requires_order": False,
                "sort_order": 90,
                "labels": {"zh": "标签"},
                "kind": BANNER_KIND,
            },
        )


@pytest.mark.parametrize(
    "image_url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:image/png;base64,AAAA",
        "//evil.com/a.png",
    ],
)
def test_a_banner_image_must_be_http_or_https(tmp_path: Path, image_url: str) -> None:
    """The value reaches a client's image loader, so it is validated at the
    boundary. `data:` is an unbounded blob through config; `javascript:` and
    `file:` are execution / local-read primitives."""
    store, manager = _seeded(tmp_path)
    with pytest.raises(ShortcutValidationError, match="image_url"):
        manager.create(
            _Ctx(),  # type: ignore[arg-type]
            {
                "business_entry": "consumer",
                "code": "bad_image",
                "intent": "knowledge",
                "requires_order": False,
                "sort_order": 90,
                "labels": {"zh": "标签"},
                "kind": BANNER_KIND,
                "jump_path": "/aiPackage/pages/batteryReport/batteryReport",
                "image_url": image_url,
            },
        )


def test_a_tenant_override_lands_on_the_same_surface(tmp_path: Path) -> None:
    """Suppressing a platform banner must not inject a stray button.

    `suppress` copies the published platform row into a tenant row and disables
    it. The copy shadows by `code`, so if it were created as a button the banner
    would stay visible AND a new button would appear in the button listing —
    two wrong surfaces from one call.

    A dedicated platform context and an empty tenant: `suppress` only reaches
    its copy path when the tenant has no row of that code yet, so seeding first
    would test the other branch.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)

    class _Platform:
        effective_tenant_id = "__platform__"
        roles = frozenset({"ROLE_PLATFORM_ADMIN"})
        caller = type("C", (), {"b_user_id": "platform-admin"})()

    created = manager.create(
        _Platform(),  # type: ignore[arg-type]
        {
            "business_entry": "consumer",
            "code": "battery_report",
            "intent": "knowledge",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "横幅"},
            "kind": BANNER_KIND,
            "jump_path": "/aiPackage/pages/batteryReport/batteryReport",
            "image_url": "https://car3.autoimg.cn/cardfs/series/example.png",
        },
        scope="platform",
    )
    manager.publish(_Platform(), created.shortcut_id, expected_revision=created.revision, scope="platform")  # type: ignore[arg-type]

    override = manager.suppress(_Ctx(), business_entry="consumer", code="battery_report")  # type: ignore[arg-type]

    assert override.status == "disabled"
    assert override.kind == BANNER_KIND, (
        "an override that lands on another surface both fails to hide the banner and injects a stray row"
    )
    assert override.image_url == "https://car3.autoimg.cn/cardfs/series/example.png"
    # The tenant now has no visible banner, and the button listing is untouched.
    assert store.list_effective("T-1", "consumer", kind=BANNER_KIND) == []
    assert store.list_effective("T-1", "consumer", kind=BUTTON_KIND) == []


def test_an_existing_database_gains_the_columns_in_place(tmp_path: Path) -> None:
    """No migration framework here: a store created before `kind`/`image_url`
    existed must be upgraded by `_initialize`, with existing rows reading as
    buttons — the surface they were already being served on."""
    path = tmp_path / "gateway.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE shortcuts (
            shortcut_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            business_entry TEXT NOT NULL,
            code TEXT NOT NULL,
            intent TEXT NOT NULL,
            requires_order INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            fields_json TEXT NOT NULL,
            published_version INTEGER,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, business_entry, code)
        );
        INSERT INTO shortcuts VALUES
            ('sct_old', 'T-1', 'consumer', 'case_exploration', 'case_exploration',
             0, 10, 'published', 1,
             '{"labels":{"zh":"客户案例"},"descriptions":{"zh":"查看案例"},"question_templates":{"zh":"看案例"}}',
             1, 'legacy', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    store = ShortcutStore(path)
    row = store.get("sct_old", "T-1")
    assert row.kind == BUTTON_KIND
    assert row.image_url is None
    # And the listing that excludes banners still returns it.
    assert [r.code for r in store.list_effective("T-1", "consumer", kind=BUTTON_KIND)] == ["case_exploration"]
    assert store.list_effective("T-1", "consumer", kind=BANNER_KIND) == []
