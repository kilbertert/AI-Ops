from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import ShortcutStore


class _Caller:
    """Grants the requested scope; token "narrow" only holds faq:read."""

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        if token == "narrow" and required_scope != "aiops:faq:read":
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        tenant_id = "T-2" if token == "tenant-2" else "T-1"
        roles = (
            {"ROLE_PLATFORM_ADMIN"}
            if token == "platform"
            else ({"ROLE_AGENT_ADMIN"} if token != "tenant-2" else set())
        )
        subject = SubjectRecord(b_user_id=f"c:{tenant_id}", c_user_id=tenant_id, tenant_id=tenant_id)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant_id,
            data_scope=DataScope(type="self"),
            roles=frozenset(roles),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        client_type = "admin" if tenant_id == "T-1" else "consumer"
        return (PlatformRoleRecord(f"B-{tenant_id}", c_user_id, tenant_id, client_type),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Runtime:
    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=lambda: None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}
_TENANT_2_HEADERS = {"Authorization": "Bearer tenant-2", "X-Business-Entry": "consumer"}
_PLATFORM_HEADERS = {"Authorization": "Bearer platform", "X-Business-Entry": "consumer"}


def _publish_initial(client: TestClient, code: str) -> dict:
    created = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": code,
            "intent": {
                "case_exploration": "case_exploration",
                "smart_diagnosis": "order_issue",
                "report_fault": "report_fault",
            }[code],
            "requires_order": code == "smart_diagnosis",
            "sort_order": {"case_exploration": 10, "smart_diagnosis": 20, "report_fault": 30}[code],
            "labels": {"zh": "按钮", "en": "Button"},
            "descriptions": {"zh": "说明"},
            "question_templates": {"zh": "模板"},
            "target_agent_version": None,
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text
    return shortcut


def test_published_visible_draft_invisible_disabled_removed(tmp_path: Path) -> None:
    client = _client(tmp_path)

    # Nothing published yet: empty listing.
    empty = client.get("/v1/shortcuts", headers=_HEADERS)
    assert empty.status_code == 200
    assert empty.json() == {
        "type": "shortcut_list",
        "language": "zh",
        "count": 0,
        "shortcuts": [],
    }

    # Draft is invisible until published.
    shortcut = _publish_initial(client, "case_exploration")
    draft_created = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "draft_only",
            "intent": "knowledge",
            "requires_order": False,
            "sort_order": 50,
            "labels": {"zh": "草稿"},
        },
    )
    assert draft_created.status_code == 201
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert [item["code"] for item in listed["shortcuts"]] == ["case_exploration"]

    # Disabled shortcuts disappear from the public listing.
    disabled = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/disable",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"] + 1},
    )
    assert disabled.status_code == 200, disabled.text
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["count"] == 0


def test_tenant_and_entry_isolation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _publish_initial(client, "case_exploration")

    # Same tenant, different entry: operator listing is empty.
    operator = client.get("/v1/shortcuts", headers={**_HEADERS, "X-Business-Entry": "operator"})
    assert operator.status_code == 200
    assert operator.json()["count"] == 0

    # Duplicate code in the SAME tenant+entry is rejected; the other entry accepts it.
    dup = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 5,
            "labels": {"zh": "重复"},
        },
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "SHORTCUT_REVISION_CONFLICT"

    other_entry = client.post(
        "/v1/shortcuts",
        headers={**_HEADERS, "X-Business-Entry": "operator"},
        json={
            "business_entry": "operator",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 5,
            "labels": {"zh": "运营端案例"},
        },
    )
    assert other_entry.status_code == 201


def test_requires_order_metadata_for_frontend(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _publish_initial(client, "smart_diagnosis")
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["count"] == 1
    smart = listed["shortcuts"][0]
    assert smart["code"] == "smart_diagnosis"
    assert smart["requires_order"] is True
    assert smart["label"] == "按钮"  # zh default (no Accept-Language)

    # An order_issue shortcut without requires_order is invalid.
    bad = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "broken_order",
            "intent": "order_issue",
            "requires_order": False,
            "sort_order": 1,
            "labels": {"zh": "缺订单"},
        },
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "SHORTCUT_VALIDATION_FAILED"


def test_language_fields_and_fallback(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例", "en": "Customer Cases"},
            "descriptions": {"zh": "查看案例"},
            "question_templates": {"zh": "看案例"},
        },
    )
    assert created.status_code == 201
    shortcut = created.json()
    client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )

    en = client.get("/v1/shortcuts", headers={**_HEADERS, "Accept-Language": "en"}).json()
    assert en["language"] == "en"
    assert en["shortcuts"][0]["label"] == "Customer Cases"
    # de has no copy: falls back to zh.
    de = client.get("/v1/shortcuts", headers={**_HEADERS, "Accept-Language": "de"}).json()
    assert de["shortcuts"][0]["label"] == "客户案例"
    assert de["shortcuts"][0]["description"] == "查看案例"


def test_management_requires_scope_and_roles(tmp_path: Path) -> None:
    client = _client(tmp_path)

    # Create/publish forbidden without the manage scope (narrow token).
    narrow_headers = {"Authorization": "Bearer narrow", "X-Business-Entry": "consumer"}
    denied = client.post(
        "/v1/shortcuts",
        headers=narrow_headers,
        json={
            "business_entry": "consumer",
            "code": "x",
            "intent": "knowledge",
            "requires_order": False,
            "sort_order": 1,
            "labels": {"zh": "无权"},
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "INSUFFICIENT_SCOPE"

    # Missing auth is a uniform 401 on both listing and management.
    anonymous = client.get("/v1/shortcuts", headers={})
    assert anonymous.status_code == 401

    # Listing itself is readable by any assistant-scope caller — the
    # consumer entry renders for everyone with a valid token.
    ok = client.get("/v1/shortcuts", headers=_HEADERS)
    assert ok.status_code == 200

    read_allowed = client.get(
        "/v1/shortcuts", headers={"Authorization": "Bearer narrow", "X-Business-Entry": "consumer"}
    )
    assert read_allowed.status_code == 200


def test_published_version_is_immutable_snapshot(tmp_path: Path) -> None:
    client = _client(tmp_path)
    shortcut = _publish_initial(client, "case_exploration")
    listed_before = client.get("/v1/shortcuts", headers=_HEADERS).json()["shortcuts"][0]

    # Fork a draft, edit the label, publish again: the listing follows v2,
    # and version 1 keeps its original snapshot.
    forked = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/draft",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"] + 1},
    )
    assert forked.status_code == 200, forked.text
    updated = client.put(
        f"/v1/shortcuts/{shortcut['shortcut_id']}",
        headers=_HEADERS,
        json={
            "expected_revision": forked.json()["revision"],
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例 v2"},
        },
    )
    assert updated.status_code == 200, updated.text
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert published.status_code == 200
    assert published.json()["version_no"] == 2

    listed_after = client.get("/v1/shortcuts", headers=_HEADERS).json()["shortcuts"][0]
    assert listed_after["label"] == "客户案例 v2"
    assert listed_before["label"] == "按钮"

    v1 = client.get(f"/v1/shortcuts/{shortcut['shortcut_id']}/versions/1", headers=_HEADERS)
    assert v1.status_code == 200
    assert v1.json()["snapshot"]["labels"]["zh"] == "按钮"


def test_platform_published_shortcut_is_visible_to_tenants_without_copy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/v1/shortcuts?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "平台案例", "en": "Platform Cases"},
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    assert shortcut["scope"] == "platform"
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text

    first = client.get("/v1/shortcuts", headers=_HEADERS)
    second = client.get("/v1/shortcuts", headers=_TENANT_2_HEADERS)
    assert first.status_code == second.status_code == 200
    assert first.json()["shortcuts"] == second.json()["shortcuts"]
    assert first.json()["shortcuts"][0]["code"] == "case_exploration"
    operator = client.get("/v1/shortcuts", headers={**_HEADERS, "X-Business-Entry": "operator"})
    assert operator.status_code == 200
    assert operator.json()["count"] == 0

    bad_target = client.post(
        "/v1/shortcuts?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "solution_discovery",
            "intent": "solution_discovery",
            "labels": {"zh": "方案"},
            "target_agent_version": "agt_12345678#v1",
        },
    )
    assert bad_target.status_code == 422
    assert bad_target.json()["error"]["code"] == "SHORTCUT_VALIDATION_FAILED"


def test_platform_management_requires_platform_role(tmp_path: Path) -> None:
    client = _client(tmp_path)
    denied = client.post(
        "/v1/shortcuts?scope=platform",
        headers=_TENANT_2_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "labels": {"zh": "无权"},
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SHORTCUT_FORBIDDEN"


def test_platform_rollback_publishes_an_immutable_previous_snapshot(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/v1/shortcuts?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "labels": {"zh": "v1"},
        },
    )
    assert created.status_code == 201
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200
    current = client.get("/v1/shortcut-admin/consumer?scope=platform", headers=_PLATFORM_HEADERS).json()
    assert current["shortcuts"][0]["revision"] == 2

    draft = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/draft?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={"expected_revision": 2},
    )
    updated = client.put(
        f"/v1/shortcuts/{shortcut['shortcut_id']}?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={
            "expected_revision": draft.json()["revision"],
            "intent": "case_exploration",
            "requires_order": False,
            "labels": {"zh": "v2"},
        },
    )
    republished = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert republished.status_code == 200
    current = client.get("/v1/shortcut-admin/consumer?scope=platform", headers=_PLATFORM_HEADERS).json()
    assert current["shortcuts"][0]["revision"] == 5

    rollback = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/rollback?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={"expected_revision": 5, "version_no": 1},
    )
    assert rollback.status_code == 200, rollback.text
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["shortcuts"][0]["label"] == "v1"
    assert rollback.json()["version_no"] == 3


def test_effective_shortcut_resolution_prefers_published_tenant_row(tmp_path: Path) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager

    manager = ShortcutManager(store)
    store.create(
        "__platform__",
        "consumer",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "平台"},
        descriptions={},
        question_templates={},
        target_agent_version=None,
        created_by="platform",
    )
    platform_row = store.find_by_code("__platform__", "consumer", "case_exploration")
    assert platform_row is not None
    manager.publish(
        type("Ctx", (), {"effective_tenant_id": "__platform__", "roles": {"ROLE_PLATFORM_ADMIN"}})(),
        platform_row.shortcut_id,
        expected_revision=platform_row.revision,
        scope="platform",
    )
    store.create(
        "T-1",
        "consumer",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=5,
        labels={"zh": "租户"},
        descriptions={},
        question_templates={},
        target_agent_version=None,
        created_by="tenant",
    )
    tenant_row = store.find_by_code("T-1", "consumer", "case_exploration")
    assert tenant_row is not None
    manager.publish(
        type("Ctx", (), {"effective_tenant_id": "T-1", "roles": {"ROLE_AGENT_ADMIN"}})(),
        tenant_row.shortcut_id,
        expected_revision=tenant_row.revision,
    )
    assert [row.labels["zh"] for row in store.list_effective("T-1", "consumer")] == ["租户"]
    assert [row.labels["zh"] for row in store.list_effective("T-2", "consumer")] == ["平台"]


def test_tenant_override_and_suppression_do_not_affect_other_tenants(tmp_path: Path) -> None:
    client = _client(tmp_path)
    platform = client.post(
        "/v1/shortcuts?scope=platform",
        headers=_PLATFORM_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "平台"},
        },
    ).json()
    assert (
        client.post(
            f"/v1/shortcuts/{platform['shortcut_id']}/publish?scope=platform",
            headers=_PLATFORM_HEADERS,
            json={"expected_revision": platform["revision"]},
        ).status_code
        == 200
    )

    override = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 5,
            "labels": {"zh": "租户覆盖"},
        },
    )
    assert override.status_code == 201
    assert (
        client.post(
            f"/v1/shortcuts/{override.json()['shortcut_id']}/publish",
            headers=_HEADERS,
            json={"expected_revision": override.json()["revision"]},
        ).status_code
        == 200
    )
    assert client.get("/v1/shortcuts", headers=_HEADERS).json()["shortcuts"][0]["label"] == "租户覆盖"
    assert client.get("/v1/shortcuts", headers=_TENANT_2_HEADERS).json()["shortcuts"][0]["label"] == "平台"

    suppressed = client.post("/v1/shortcut-admin/consumer/case_exploration/suppress", headers=_HEADERS)
    assert suppressed.status_code == 200, suppressed.text
    assert suppressed.json()["status"] == "disabled"
    assert client.get("/v1/shortcuts", headers=_HEADERS).json()["count"] == 0
    assert client.get("/v1/shortcuts", headers=_TENANT_2_HEADERS).json()["count"] == 1

    restored = client.post("/v1/shortcut-admin/consumer/case_exploration/restore", headers=_HEADERS)
    assert restored.status_code == 200, restored.text
    assert client.get("/v1/shortcuts", headers=_HEADERS).json()["shortcuts"][0]["label"] == "租户覆盖"


def test_tenant_cannot_suppress_platform_scope_or_other_entry(tmp_path: Path) -> None:
    client = _client(tmp_path)
    denied = client.post("/v1/shortcut-admin/operator/case_exploration/suppress", headers=_TENANT_2_HEADERS)
    assert denied.status_code == 403


def test_store_seed_bundled_is_idempotent(tmp_path: Path) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")

    class _Ctx:
        effective_tenant_id = "T-1"
        caller = type("C", (), {"b_user_id": "seed-user"})()

    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager

    manager = ShortcutManager(store)
    first = store.seed_bundled(_Ctx(), manager)  # type: ignore[arg-type]
    # Codes repeat across entries (identity is tenant+entry+code), so the seed
    # is asserted per entry: consumer keeps its three, operator holds the two
    # prompt actions the PRD names and no jump action.
    assert {(row.business_entry, row.code) for row in first} == {
        ("consumer", "case_exploration"),
        ("consumer", "report_fault"),
        ("consumer", "smart_diagnosis"),
        ("operator", "case_exploration"),
        ("operator", "smart_diagnosis"),
    }
    # Second run creates nothing.
    assert store.seed_bundled(_Ctx(), manager) == []  # type: ignore[arg-type]


def _publish_jump_shortcut(client: TestClient, code: str, jump_path: str) -> dict:
    created = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": code,
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 30,
            "labels": {"zh": "故障上报", "en": "Report a Fault"},
            "question_templates": {"zh": "我要上报一个故障"},
            "jump_path": jump_path,
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text
    return shortcut


def test_jump_path_round_trips_and_non_jump_actions_are_null(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _publish_jump_shortcut(client, "report_fault", "/charge/pages/faultReport/faultReportList")
    _publish_initial(client, "case_exploration")

    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    by_code = {item["code"]: item for item in listed["shortcuts"]}

    # Present and equal on the jump action.
    assert by_code["report_fault"]["jump_path"] == "/charge/pages/faultReport/faultReportList"
    # Present-but-null on the prompt action: the frontend must not need to
    # distinguish "absent" from "empty".
    assert "jump_path" in by_code["case_exploration"]
    assert by_code["case_exploration"]["jump_path"] is None


def test_jump_path_absent_in_request_reads_as_null(tmp_path: Path) -> None:
    """Existing published rows predate the field and must not need a republish."""
    client = _client(tmp_path)
    _publish_initial(client, "case_exploration")  # helper never sends jump_path

    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["shortcuts"][0]["jump_path"] is None
    # Everything else is unchanged.
    assert listed["shortcuts"][0]["code"] == "case_exploration"


def test_jump_path_must_start_with_slash_and_respect_length(tmp_path: Path) -> None:
    client = _client(tmp_path)

    def _create(jump_path: object) -> object:
        return client.post(
            "/v1/shortcuts",
            headers=_HEADERS,
            json={
                "business_entry": "consumer",
                "code": "report_fault",
                "intent": "report_fault",
                "requires_order": False,
                "sort_order": 30,
                "labels": {"zh": "故障上报"},
                "jump_path": jump_path,
            },
        )

    no_slash = _create("charge/pages/faultReport")
    assert no_slash.status_code == 422

    too_long = _create("/" + "a" * 512)
    assert too_long.status_code == 422

    at_limit = _create("/" + "a" * 511)
    assert at_limit.status_code == 201, at_limit.text

    # Omitted entirely is valid and means a prompt action.
    omitted = client.post(
        "/v1/shortcuts",
        headers={**_HEADERS, "X-Business-Entry": "operator"},
        json={
            "business_entry": "operator",
            "code": "report_fault",
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 30,
            "labels": {"zh": "故障上报"},
        },
    )
    assert omitted.status_code == 201, omitted.text
    assert omitted.json()["jump_path"] is None


def test_jump_path_is_frozen_into_version_and_change_needs_new_version(tmp_path: Path) -> None:
    client = _client(tmp_path)
    shortcut = _publish_jump_shortcut(client, "report_fault", "/charge/pages/faultReport/faultReportList")

    v1 = client.get(f"/v1/shortcuts/{shortcut['shortcut_id']}/versions/1", headers=_HEADERS).json()
    assert v1["snapshot"]["jump_path"] == "/charge/pages/faultReport/faultReportList"

    # Editing a published action requires forking a draft first, so the
    # already-published snapshot keeps rendering for in-flight clients.
    forked = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/draft",
        headers=_HEADERS,
        json={"expected_revision": shortcut["revision"] + 1},
    )
    assert forked.status_code == 200, forked.text
    draft = forked.json()
    assert draft["jump_path"] == "/charge/pages/faultReport/faultReportList"

    updated = client.put(
        f"/v1/shortcuts/{shortcut['shortcut_id']}",
        headers=_HEADERS,
        json={
            "expected_revision": draft["revision"],
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 30,
            "labels": {"zh": "故障上报"},
            "jump_path": "/charge/pages/faultReport/other",
        },
    )
    assert updated.status_code == 200, updated.text

    # A forked draft contributes nothing to the effective merge, so a
    # tenant-only action is absent from the listing during the edit window.
    # (Pre-existing lifecycle semantics, not introduced by jump_path: with a
    # published platform default present, that default shows through instead.)
    during_edit = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert during_edit["count"] == 0

    promoted = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert promoted.status_code == 200, promoted.text
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["shortcuts"][0]["jump_path"] == "/charge/pages/faultReport/other"

    # v1 is immutable.
    v1_again = client.get(f"/v1/shortcuts/{shortcut['shortcut_id']}/versions/1", headers=_HEADERS).json()
    assert v1_again["snapshot"]["jump_path"] == "/charge/pages/faultReport/faultReportList"


def test_jump_path_validation_rejects_non_string(tmp_path: Path) -> None:
    store = ShortcutStore(tmp_path / "gateway.db")
    from aiops_diagnostics.shortcut_lifecycle import (
        ShortcutManager,
        ShortcutValidationError,
    )

    manager = ShortcutManager(store)

    class _Ctx:
        effective_tenant_id = "T-1"
        roles = frozenset({"ROLE_AGENT_ADMIN"})
        caller = type("C", (), {"b_user_id": "admin"})()

    for bad in (123, "", "  ", "/x"):
        payload = {
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 1,
            "labels": {"zh": "故障上报"},
            "jump_path": bad,
        }
        if bad in ("", "  "):
            # Whitespace-only is treated as "no path", not an error.
            fields = manager._validated_fields(payload, for_publish=False)
            assert fields["jump_path"] is None
        elif bad == "/x":
            fields = manager._validated_fields(payload, for_publish=False)
            assert fields["jump_path"] == "/x"
        else:
            try:
                manager._validated_fields(payload, for_publish=False)
            except ShortcutValidationError:
                pass
            else:  # pragma: no cover
                raise AssertionError("non-string jump_path must be rejected")


def test_bundled_seed_carries_the_product_fault_report_path(tmp_path: Path) -> None:
    """The product default path is a repo asset, not an ad-hoc migration string."""
    from aiops_diagnostics.shortcut_lifecycle import REPORT_FAULT_JUMP_PATH

    store = ShortcutStore(tmp_path / "gateway.db")

    class _Ctx:
        effective_tenant_id = "T-1"
        caller = type("C", (), {"b_user_id": "seed-user"})()

    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager

    seeded = store.seed_bundled(_Ctx(), ShortcutManager(store))  # type: ignore[arg-type]
    by_code = {s.code: s for s in seeded}
    assert by_code["report_fault"].jump_path == REPORT_FAULT_JUMP_PATH
    # The prompt actions stay prompt actions.
    assert by_code["case_exploration"].jump_path is None
    assert by_code["smart_diagnosis"].jump_path is None


def test_jump_path_and_promo_target_are_mutually_exclusive(tmp_path: Path) -> None:
    """A jump action never reaches its agent, so a promo pin would be dead
    config with a live side effect: the pin marks an agent promotional, so it
    would keep excluding that agent from customer-agent selection for a
    response nothing ever fetches."""
    client = _client(tmp_path)
    both = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例"},
            "target_agent_version": "agt_12345678#v1",
            "jump_path": "/charge/pages/cases/list",
        },
    )
    assert both.status_code == 422
    assert both.json()["error"]["code"] == "SHORTCUT_VALIDATION_FAILED"

    # Either one alone is still fine.
    promo_only = client.post(
        "/v1/shortcuts",
        headers=_HEADERS,
        json={
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例"},
            "target_agent_version": "agt_12345678#v1",
        },
    )
    assert promo_only.status_code == 201, promo_only.text

    jump_only = client.post(
        "/v1/shortcuts",
        headers={**_HEADERS, "X-Business-Entry": "operator"},
        json={
            "business_entry": "operator",
            "code": "report_fault",
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 30,
            "labels": {"zh": "故障上报"},
            "jump_path": "/charge/pages/faultReport/faultReportList",
        },
    )
    assert jump_only.status_code == 201, jump_only.text


def test_update_omitting_jump_path_preserves_it(tmp_path: Path) -> None:
    """Regression: the request model defaults jump_path to None, so an
    unchanged edit used to silently demote a jump action to a prompt action —
    the one field that discriminates the two destroyed by editing a label."""
    client = _client(tmp_path)
    created = _publish_jump_shortcut(client, "report_fault", "/charge/pages/faultReport/faultReportList")
    forked = client.post(
        f"/v1/shortcuts/{created['shortcut_id']}/draft",
        headers=_HEADERS,
        json={"expected_revision": created["revision"] + 1},
    ).json()

    # Change ONLY the copy; do not mention jump_path at all.
    updated = client.put(
        f"/v1/shortcuts/{created['shortcut_id']}",
        headers=_HEADERS,
        json={
            "expected_revision": forked["revision"],
            "intent": "report_fault",
            "requires_order": False,
            "sort_order": 30,
            "labels": {"zh": "故障上报（新文案）"},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["jump_path"] == "/charge/pages/faultReport/faultReportList"

    promoted = client.post(
        f"/v1/shortcuts/{created['shortcut_id']}/publish",
        headers=_HEADERS,
        json={"expected_revision": updated.json()["revision"]},
    )
    assert promoted.status_code == 200, promoted.text
    listed = client.get("/v1/shortcuts", headers=_HEADERS).json()
    assert listed["shortcuts"][0]["jump_path"] == "/charge/pages/faultReport/faultReportList"
    assert listed["shortcuts"][0]["label"] == "故障上报（新文案）"


def test_update_jump_path_semantics_preserve_vs_clear(tmp_path: Path) -> None:
    """The three spellings must be coherent, since they differ in meaning:
    absent = unchanged, explicit None or whitespace = clear (a prompt action),
    a real path = set. Absent is the one the HTTP model can send by accident,
    so it is the one that must not destroy state."""
    store = ShortcutStore(tmp_path / "gateway.db")
    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager

    manager = ShortcutManager(store)

    class _Ctx:
        effective_tenant_id = "T-1"
        roles = frozenset({"ROLE_AGENT_ADMIN"})
        caller = type("C", (), {"b_user_id": "admin"})()

    ctx = _Ctx()
    base = {
        "business_entry": "consumer",
        "code": "report_fault",
        "intent": "report_fault",
        "requires_order": False,
        "sort_order": 30,
        "labels": {"zh": "故障上报"},
        "jump_path": "/charge/pages/faultReport/faultReportList",
    }
    created = manager.create(ctx, base)  # type: ignore[arg-type]

    def _edit(payload: dict, revision: int):
        # update is replace-semantics: every other field must be resent, which
        # is exactly the situation that made the omission bug reachable.
        return manager.update(
            ctx,  # type: ignore[arg-type]
            created.shortcut_id,
            {
                "expected_revision": revision,
                "intent": "report_fault",
                "requires_order": False,
                "sort_order": 30,
                "labels": {"zh": "改"},
                **payload,
            },
        )

    # (a) key absent -> unchanged  (the case the HTTP model sends by accident)
    step = _edit({}, created.revision)
    assert step.jump_path == "/charge/pages/faultReport/faultReportList"
    # (b) whitespace-only -> cleared, per the documented "no path" meaning
    step = _edit({"jump_path": "   "}, step.revision)
    assert step.jump_path is None
    # (c) explicit None -> cleared (already clear, stays clear)
    step = _edit({"jump_path": None}, step.revision)
    assert step.jump_path is None
    # (d) new path -> set
    step = _edit({"jump_path": "/charge/pages/other"}, step.revision)
    assert step.jump_path == "/charge/pages/other"


def test_http_layer_rejects_whitespace_in_jump_path(tmp_path: Path) -> None:
    """Both layers must agree: raw whitespace is not a valid URL path."""
    client = _client(tmp_path)
    for probe in ("", "  ", "/x ", " /x", "/x\n"):
        r = client.post(
            "/v1/shortcuts",
            headers=_HEADERS,
            json={
                "business_entry": "consumer",
                "code": "report_fault",
                "intent": "report_fault",
                "requires_order": False,
                "labels": {"zh": "x"},
                "jump_path": probe,
            },
        )
        assert r.status_code == 422, f"{probe!r} should be rejected, got {r.status_code}"


def test_jump_path_rejects_network_path_reference(tmp_path: Path) -> None:
    """RFC 3986 §4.2: a leading "//" is an authority, not a path, so
    "//evil.com" is a cross-host reference — a classic open-redirect vector
    once a client hands it to its navigator. Only path-absolute routes are
    acceptable for an in-app jump."""
    client = _client(tmp_path)

    def _create(jump_path: str, code: str):
        return client.post(
            "/v1/shortcuts",
            headers=_HEADERS,
            json={
                "business_entry": "consumer",
                "code": code,
                "intent": "report_fault",
                "requires_order": False,
                "labels": {"zh": "故障上报"},
                "jump_path": jump_path,
            },
        )

    for index, hostile in enumerate(("//evil.com", "//evil.com/path", "//", "///x")):
        r = _create(hostile, f"hostile_{index}")
        assert r.status_code == 422, f"{hostile!r} must be rejected, got {r.status_code}"

    # A legitimate path-absolute route still works, including one with an
    # internal double slash (only the leading "//" is an authority).
    assert _create("/charge/pages/faultReport/faultReportList", "ok_a").status_code == 201
    assert _create("/x//y", "ok_b").status_code == 201


def test_lifecycle_validator_also_rejects_network_path_reference(tmp_path: Path) -> None:
    """The operator runbook calls the lifecycle layer directly, bypassing HTTP,
    so the same guard must live there too."""
    from aiops_diagnostics.shortcut_lifecycle import (
        ShortcutManager,
        ShortcutValidationError,
    )

    manager = ShortcutManager(ShortcutStore(tmp_path / "gateway.db"))
    payload = {
        "intent": "report_fault",
        "requires_order": False,
        "sort_order": 30,
        "labels": {"zh": "故障上报"},
        "jump_path": "//evil.com",
    }
    try:
        manager._validated_fields(payload, for_publish=False)
    except ShortcutValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("//evil.com must be rejected by the lifecycle validator")


def test_bundled_seed_copy_covers_every_supported_language() -> None:
    """41 live (2026-09-18): the seed carried zh+en only, so a de/fr/es/pt user
    got Chinese buttons while the response still echoed their language.

    public() falls back to zh per field, so a missing language is silent: the
    request succeeds and looks localized."""
    from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
    from aiops_diagnostics.shortcut_lifecycle import _BUNDLED_SHORTCUTS

    checked = 0
    for entry, specs in _BUNDLED_SHORTCUTS:
        assert entry in {"consumer", "operator"}
        for code, spec in specs.items():
            for field in ("labels", "descriptions", "question_templates"):
                copy = spec.get(field) or {}
                missing = [lang for lang in SUPPORTED_LANGUAGES if lang not in copy]
                assert not missing, f"{entry}/{code}/{field} missing: {missing}"
                for lang in SUPPORTED_LANGUAGES:
                    assert str(copy[lang]).strip(), f"{entry}/{code}/{field}/{lang} is empty"
                checked += 1
    assert checked >= 3, "seed shape changed; update this test deliberately"


def test_live_rows_report_missing_translations_instead_of_falling_back_silently(
    caplog,
) -> None:
    """The seed check covers NEW rows; this covers rows already in the store.

    41 live: the copy gap that survived a release cycle was in existing rows
    created from the old zh+en seed. public() still falls back to zh (returning
    empty would blank the user's buttons) but a fallback must never be silent —
    that silence is why the defect was invisible until a customer saw it.
    """
    import logging

    from aiops_diagnostics.shortcut_lifecycle import Shortcut

    shortcut = Shortcut(
        shortcut_id="sct_test",
        tenant_id="tenant-1",
        business_entry="consumer",
        code="smart_diagnosis",
        intent="order_issue",
        requires_order=True,
        sort_order=10,
        status="published",
        revision=1,
        labels={"zh": "智能检测", "en": "Smart Diagnosis"},
        descriptions={"zh": "选择订单后自动诊断充电异常", "en": "Diagnose a charging issue"},
        # de/fr/es/pt were never translated on this row.
        question_templates={"zh": "帮我检测这个订单的充电异常", "en": "Diagnose this order"},
        target_agent_version=None,
        jump_path=None,
        published_version=1,
        created_by="tester",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )

    with caplog.at_level(logging.WARNING, logger="aiops_diagnostics.shortcut_lifecycle"):
        listed = shortcut.public("fr")

    # The user still gets usable copy rather than a blank button.
    assert listed["label"] == "智能检测"
    assert listed["question_template"] == "帮我检测这个订单的充电异常"

    # ...and the gap is recorded, field by field, without dumping the copy.
    warnings = [r for r in caplog.records if getattr(r, "event", "") == "shortcut_translation_missing"]
    assert {r.field for r in warnings} == {"label", "description", "question_template"}
    assert all(r.language == "fr" for r in warnings)
    assert all(r.code == "smart_diagnosis" for r in warnings)
    assert "智能检测" not in caplog.text


def test_a_fully_translated_row_warns_about_nothing(caplog) -> None:
    import logging

    from aiops_diagnostics.shortcut_lifecycle import Shortcut

    shortcut = Shortcut(
        shortcut_id="sct_ok",
        tenant_id="tenant-1",
        business_entry="consumer",
        code="report_fault",
        intent="report_fault",
        requires_order=False,
        sort_order=30,
        status="published",
        revision=1,
        labels={"zh": "故障上报", "en": "Report a Fault"},
        descriptions={"zh": "描述故障现象", "en": "Describe the fault"},
        question_templates={"zh": "我要上报一个故障", "en": "I want to report a fault"},
        target_agent_version=None,
        jump_path="/charge/pages/faultReport/faultReportList",
        published_version=1,
        created_by="tester",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )

    with caplog.at_level(logging.WARNING, logger="aiops_diagnostics.shortcut_lifecycle"):
        shortcut.public("en")

    assert not [r for r in caplog.records if getattr(r, "event", "") == "shortcut_translation_missing"]


def test_the_default_language_is_never_reported_as_a_gap(caplog) -> None:
    """zh is the authority: reading it is not a fallback."""
    import logging

    from aiops_diagnostics.shortcut_lifecycle import Shortcut

    shortcut = Shortcut(
        shortcut_id="sct_zh",
        tenant_id="tenant-1",
        business_entry="consumer",
        code="report_fault",
        intent="report_fault",
        requires_order=False,
        sort_order=30,
        status="published",
        revision=1,
        labels={"zh": "故障上报"},
        descriptions={"zh": "描述故障现象"},
        question_templates={"zh": "我要上报一个故障"},
        target_agent_version=None,
        jump_path=None,
        published_version=1,
        created_by="tester",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )

    with caplog.at_level(logging.WARNING, logger="aiops_diagnostics.shortcut_lifecycle"):
        shortcut.public("zh")

    assert not [r for r in caplog.records if getattr(r, "event", "") == "shortcut_translation_missing"]


def test_copy_gap_gate_names_every_missing_field_and_language() -> None:
    """The CI gate: a partial row is a defect, named precisely.

    This is the check that would have caught the live gap before a user saw it.
    It reports WHAT is missing rather than a bare boolean, so the fix is
    actionable without re-deriving the gap by hand.
    """
    from aiops_diagnostics.shortcut_lifecycle import Shortcut, shortcut_copy_gaps

    row = Shortcut(
        shortcut_id="sct_gap",
        tenant_id="tenant-1",
        business_entry="consumer",
        code="solution_discovery",
        intent="solution_discovery",
        requires_order=False,
        sort_order=40,
        status="published",
        revision=1,
        labels={"zh": "行业方案", "en": "Industry Solutions"},
        descriptions={"zh": "发现解决方案"},  # no en at all
        question_templates={"zh": "我想看看行业解决方案", "en": "Show me industry solutions"},
        target_agent_version=None,
        jump_path=None,
        published_version=1,
        created_by="tester",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )

    gaps = shortcut_copy_gaps([row])

    assert {gap.code for gap in gaps} == {"solution_discovery"}
    # All three copy fields lack de/fr/es/pt; `description` lacks en as well,
    # which is the field that lines the fixture up with the live gap.
    assert {gap.field for gap in gaps} == {"label", "description", "question_template"}
    assert {gap.language for gap in gaps} == {"de", "fr", "es", "pt", "en"}
    missing_pairs = {(gap.field, gap.language) for gap in gaps}
    assert ("description", "en") in missing_pairs
    assert ("label", "en") not in missing_pairs
    # zh is the authority, never a gap.
    assert all(gap.language != "zh" for gap in gaps)


def test_copy_gap_gate_is_empty_for_a_complete_row() -> None:
    from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
    from aiops_diagnostics.shortcut_lifecycle import Shortcut, shortcut_copy_gaps

    row = Shortcut(
        shortcut_id="sct_ok2",
        tenant_id="tenant-1",
        business_entry="consumer",
        code="report_fault",
        intent="report_fault",
        requires_order=False,
        sort_order=30,
        status="published",
        revision=1,
        labels={lang: f"label-{lang}" for lang in SUPPORTED_LANGUAGES},
        descriptions={lang: f"description-{lang}" for lang in SUPPORTED_LANGUAGES},
        question_templates={lang: f"template-{lang}" for lang in SUPPORTED_LANGUAGES},
        target_agent_version=None,
        jump_path=None,
        published_version=1,
        created_by="tester",
        created_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )

    assert shortcut_copy_gaps([row]) == []
