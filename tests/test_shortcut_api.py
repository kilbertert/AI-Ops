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
    assert sorted(s.code for s in first) == ["case_exploration", "report_fault", "smart_diagnosis"]
    # Second run creates nothing.
    assert store.seed_bundled(_Ctx(), manager) == []  # type: ignore[arg-type]
