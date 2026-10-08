"""`GET /v1/banner`: the chat-page banner's read surface (#589).

The banner is not a second resource — it is a row in `shortcuts` with the same
lifecycle, served on its own surface. These tests pin the three properties that
make it safe to hand to a client:

* **It answers the same thing for everyone.** No order, no vehicle, no session
  value. That is not an optimization, it is the design: the moment it answers
  per-person it stops being an operations slot and becomes an authorization
  query. The test drives it with a request carrying only what is needed to be
  authenticated at all.
* **It is a jump action.** `jump_path` goes out verbatim in every language, and
  `question_template` never appears — a jump action does not reach the unified
  assistant entry, so a preset prompt would be a field a client could misuse.
* **No banner is a normal state.** Empty list, 200. Not 404, not 5xx, because
  the client's rule is to draw nothing rather than half a card.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

CONSUMER_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}
OPERATOR_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
#: What a real device sends. No admin role, no manage scope — the banner is a
#: product surface every authenticated user sees.
DEVICE_HEADERS = {"Authorization": "Bearer device", "X-Business-Entry": "consumer"}


class _Caller:
    """Grants the requested scope; "narrow" holds only faq:read."""

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, source_key, platform_entry
        if token == "narrow" and required_scope != "aiops:faq:read":
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        # A device token carries NO roles: this is the caller the banner must
        # serve, and the shortcut endpoints already accept it.
        roles = set() if token == "device" else {"ROLE_AGENT_ADMIN"}
        subject = SubjectRecord(b_user_id="c:T-1", c_user_id="T-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(roles),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Runtime:
    def shutdown(self) -> None:
        pass


class _Ctx:
    effective_tenant_id = "T-1"
    roles = frozenset({"ROLE_AGENT_ADMIN"})
    caller = type("C", (), {"b_user_id": "seed-user"})()


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_Caller(),
            order_authorizer=lambda: None,  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
    )


def _publish_seed(tmp_path: Path, entries: tuple[str, ...] = ("consumer",)) -> None:
    """Publish the seeded rows for the requested entries.

    Uses the real lifecycle rather than writing rows directly: publishing is
    what makes a row visible, and a fixture that skipped it would test a state
    the product cannot reach.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    for row in store.seed_bundled(_Ctx(), manager):  # type: ignore[arg-type]
        if row.business_entry in entries:
            manager.publish(_Ctx(), row.shortcut_id, expected_revision=row.revision)  # type: ignore[arg-type]


def test_no_banner_is_an_empty_list_not_a_404(tmp_path: Path) -> None:
    """Before anything is published, the endpoint answers 200 with nothing.

    A 404 would tell the client "this request was wrong"; the truth is "there
    is no banner today", which is an ordinary state an operations team controls.
    """
    with _client(tmp_path) as client:
        response = client.get("/v1/banner", headers=CONSUMER_HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {"type": "banner", "language": "zh", "count": 0, "banners": []}


def test_the_banner_is_served_on_its_own_surface(tmp_path: Path) -> None:
    """And the button listing is untouched by its presence."""
    _publish_seed(tmp_path)
    with _client(tmp_path) as client:
        banner = client.get("/v1/banner", headers=CONSUMER_HEADERS).json()
        buttons = client.get("/v1/shortcuts", headers=CONSUMER_HEADERS).json()

    assert banner["count"] == 1
    assert [item["code"] for item in banner["banners"]] == ["battery_report"]
    assert [item["code"] for item in buttons["shortcuts"]] == [
        "case_exploration",
        "smart_diagnosis",
        "report_fault",
    ]


def test_the_banner_carries_no_preset_prompt_and_no_agent_pin(tmp_path: Path) -> None:
    """A jump action never reaches the assistant entry.

    `question_template` is a product rule, not a trimming choice: it is the
    field a client would submit to `/v1/assistant/questions`, and a banner must
    never take that path.
    """
    _publish_seed(tmp_path)
    with _client(tmp_path) as client:
        entry = client.get("/v1/banner", headers=CONSUMER_HEADERS).json()["banners"][0]

    assert set(entry) == {"code", "language", "label", "description", "jump_path", "image_url"}
    assert "question_template" not in entry
    assert "target_agent_version" not in entry
    assert "requires_order" not in entry
    # `image_url` is the operator's fallback and ships null until they set one.
    assert entry["image_url"] is None


def test_the_jump_path_is_verbatim_in_every_language(tmp_path: Path) -> None:
    """The route is one path for all languages (ADR-0006), never localized."""
    _publish_seed(tmp_path)
    with _client(tmp_path) as client:
        paths = {
            client.get("/v1/banner", headers={**CONSUMER_HEADERS, "Accept-Language": language}).json()[
                "banners"
            ][0]["jump_path"]
            for language in SUPPORTED_LANGUAGES
        }

    assert paths == {"/aiPackage/pages/batteryReport/batteryReport"}


def test_the_banner_serves_every_language_without_a_silent_fallback(tmp_path: Path) -> None:
    """The same eleven-language rule as the buttons, asserted per language.

    A response that echoes the requested language while sending Chinese is the
    defect this whole workstream exists to remove — it raises nothing and is
    visible only to the reader.
    """
    _publish_seed(tmp_path)
    with _client(tmp_path) as client:
        for language in SUPPORTED_LANGUAGES:
            body = client.get("/v1/banner", headers={**CONSUMER_HEADERS, "Accept-Language": language}).json()
            assert body["language"] == language, f"{language} 下服务了 {body['language']}"
            entry = body["banners"][0]
            assert entry["language"] == language
            assert entry["label"].strip()
            assert entry["description"].strip()


def test_a_device_token_with_no_roles_can_read_it(tmp_path: Path) -> None:
    """This is a product surface, so the auth is the assistant's read scope.

    Pinned because the natural mistake is to reuse the management dependency:
    that would lock the chat page out of its own banner, since a device token
    holds no roles at all.

    `narrow` holds only `aiops:faq:read` — the same scope the assistant read
    endpoints require — and is deliberately asserted as ALLOWED. It is the
    boundary in the other direction: the listing must not have quietly become
    an admin-only surface.

    The request without `X-Business-Entry` is 409 (the platform cannot be
    determined uniquely), matching the FAQ line rather than inventing a new
    status for the same condition.
    """
    _publish_seed(tmp_path)
    with _client(tmp_path) as client:
        allowed = client.get("/v1/banner", headers=DEVICE_HEADERS)
        read_only = client.get(
            "/v1/banner", headers={"Authorization": "Bearer narrow", "X-Business-Entry": "consumer"}
        )
        anonymous = client.get("/v1/banner", headers={"X-Business-Entry": "consumer"})
        ambiguous = client.get("/v1/banner", headers={"Authorization": "Bearer service"})

    assert allowed.status_code == 200, allowed.text
    assert read_only.status_code == 200, read_only.text
    assert anonymous.status_code == 401
    assert ambiguous.status_code == 409


def test_the_operator_entry_has_no_banner(tmp_path: Path) -> None:
    """A banner is a consumer chat-page card; the butler entry has no chat page.

    Publishing both entries keeps the assertion honest — the emptiness comes
    from the banner being a consumer-only row, not from nothing being published.
    """
    _publish_seed(tmp_path, entries=("consumer", "operator"))
    with _client(tmp_path) as client:
        operator = client.get("/v1/banner", headers=OPERATOR_HEADERS)

    assert operator.status_code == 200
    assert operator.json()["count"] == 0
    assert operator.json()["banners"] == []


def test_a_draft_banner_is_invisible(tmp_path: Path) -> None:
    """Same lifecycle as the buttons: published only."""
    store = ShortcutStore(tmp_path / "gateway.db")
    ShortcutManager(store)
    # Seeded, never published.
    store.seed_bundled(_Ctx(), ShortcutManager(store))  # type: ignore[arg-type]
    with _client(tmp_path) as client:
        response = client.get("/v1/banner", headers=CONSUMER_HEADERS)

    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_a_disabled_banner_disappears(tmp_path: Path) -> None:
    _publish_seed(tmp_path)
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    row = store.find_by_code("T-1", "consumer", "battery_report")
    assert row is not None
    manager.disable(_Ctx(), row.shortcut_id, expected_revision=row.revision)  # type: ignore[arg-type]

    with _client(tmp_path) as client:
        response = client.get("/v1/banner", headers=CONSUMER_HEADERS)

    assert response.json() == {"type": "banner", "language": "zh", "count": 0, "banners": []}


def test_the_response_is_identical_for_two_different_callers(tmp_path: Path) -> None:
    """The design assertion, stated as a test.

    Nothing in this response may depend on WHO is asking: it is static
    configuration. If this ever fails, the endpoint has started answering
    per-person and has stopped being an operations slot.
    """
    _publish_seed(tmp_path)

    class _OtherCaller(_Caller):
        def resolve(self, token: str, **kwargs) -> ScopeContext:
            ctx = super().resolve(token, **kwargs)
            other = SubjectRecord(b_user_id="c:T-9", c_user_id="T-9", tenant_id="T-1")
            return ScopeContext.build(
                caller=other,
                subject=other,
                delegated=False,
                effective_tenant_id="T-1",
                data_scope=DataScope(type="self"),
                roles=ctx.roles,
                permissions=ctx.permissions,
            )

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    with TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_OtherCaller(),
            order_authorizer=lambda: None,  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
    ) as client:
        other = client.get("/v1/banner", headers=CONSUMER_HEADERS).json()

    with _client(tmp_path) as client:
        first = client.get("/v1/banner", headers=CONSUMER_HEADERS).json()

    assert other == first
