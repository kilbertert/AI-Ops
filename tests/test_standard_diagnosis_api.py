"""External HTTP seam tests for the standard single-question diagnosis API.

These tests are deliberately written from the outside-in: they build a
``TestClient`` with only the surface that the production wiring exposes
(auth resolver, order authorizer, gateway runtime, gateway store) so that
the implementation can change without breaking the contract.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Resolver:
    """Resolves a Bearer token to an immutable ``ScopeContext``.

    Different ``b_user_id`` values produce different ``scope_fingerprint`` values,
    so the test suite can verify cross-caller isolation by switching the
    resolver.
    """

    def __init__(self, b_user_id: str = "B-1") -> None:
        self.b_user_id = b_user_id

    def resolve(self, token: str, *, required_scope: str) -> ScopeContext:
        if token == "orders-only":
            from aiops_diagnostics.caller_auth import CallerAuthError

            raise CallerAuthError("scope missing", code="caller_auth.forbidden")
        subject = SubjectRecord(b_user_id=self.b_user_id, c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _OtherResolver(_Resolver):
    def __init__(self) -> None:
        super().__init__(b_user_id="B-OTHER")


class _Orders:
    """Authorizes only ``O-1``; everything else looks like 'not found'."""

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no == "O-1"


class _Runtime:
    """Fake runtime that simulates the diagnosis worker without touching Codex/DB."""

    def __init__(self, store: GatewayStore) -> None:
        self.store = store
        self.started: list[str] = []

    def start_standard_diagnosis(
        self,
        context: ScopeContext,
        order_no: str,
        question: str,
        indicator_code: str | None,
        language: str = "zh",
    ) -> dict:
        self.started.append(order_no)
        return self.store.create_standard_diagnosis(
            context.scope_fingerprint,
            order_no,
            question,
            indicator_code,
        )

    def get_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str) -> dict | None:
        return self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)

    def list_standard_diagnoses(self, context: ScopeContext, *, limit: int) -> list[dict]:
        return self.store.list_standard_diagnoses(context.scope_fingerprint, limit=limit)

    def shutdown(self) -> None:
        pass


def _client(
    tmp_path: Path,
    *,
    resolver: _Resolver | None = None,
) -> tuple[TestClient, GatewayStore, _Runtime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    runtime = _Runtime(store)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=resolver or _Resolver(),
        order_authorizer=_Orders(),
    )
    return TestClient(app), store, runtime


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_create_diagnosis_returns_202_with_opaque_id_and_retry_after(tmp_path: Path) -> None:
    client, store, runtime = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "为什么跳枪了"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["diagnosis_id"].startswith("dx_")
    assert body["order_no"] == "O-1"
    assert body["status"] == "queued"
    assert body["retry_after_ms"] == 1000
    assert "scope_fingerprint" not in body
    assert runtime.started == ["O-1"]


def test_create_diagnosis_rejects_unauthorized_order(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-OTHER", "question": "test"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert store.count_standard_diagnoses() == 0


def test_create_diagnosis_rejects_bare_identity_headers(tmp_path: Path) -> None:
    """Client-supplied ``user_id`` / ``tenant_id`` are rejected by the schema.

    The Pydantic model uses ``extra="forbid"`` so the resolver alone builds the
    ``ScopeContext``; identity headers from the client never reach the
    business logic.
    """
    client, store, _ = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={
                "order_no": "O-1",
                "question": "test",
                "user_id": "B-EVIL",
                "tenant_id": "T-EVIL",
            },
        )

    assert response.status_code == 422
    # The injected fields must not have created any record.
    assert store.count_standard_diagnoses() == 0


def test_get_diagnosis_returns_state_only(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        ).json()
        diagnosis_id = created["diagnosis_id"]
        # simulate worker progress
        store.update_standard_diagnosis(diagnosis_id, status="running")
        store.update_standard_diagnosis(
            diagnosis_id,
            status="completed",
            result={
                "status": "diagnosed",
                "confidence": "high",
                "summary": "ok",
                "evidence_count": 3,
            },
        )
        response = client.get(
            f"/v1/standard/diagnoses/{diagnosis_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"]["summary"] == "ok"
    # Caller-internal fields must not leak.
    for forbidden in ("scope_fingerprint", "workspace_id", "provider", "fixture", "evidence"):
        assert forbidden not in body


def test_get_diagnosis_isolated_by_scope(tmp_path: Path) -> None:
    """The same ``diagnosis_id`` from another caller must be 404, not 200/403."""
    client, store, _ = _client(tmp_path, resolver=_Resolver(b_user_id="B-1"))
    with client:
        created = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        ).json()
        diagnosis_id = created["diagnosis_id"]

    other_client, _, _ = _client(tmp_path, resolver=_OtherResolver())
    with other_client:
        response = other_client.get(
            f"/v1/standard/diagnoses/{diagnosis_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"


def test_get_diagnosis_random_id_is_not_found(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get(
            "/v1/standard/diagnoses/dx_random_garbage",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"


def test_list_diagnoses_isolated_by_scope(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path, resolver=_Resolver(b_user_id="B-1"))
    with client:
        for q in ("q1", "q2", "q3"):
            client.post(
                "/v1/standard/diagnoses",
                headers={"Authorization": "Bearer token"},
                json={"order_no": "O-1", "question": q},
            )

    other_client, _, _ = _client(tmp_path, resolver=_OtherResolver())
    with client:
        mine = client.get(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
        )
    with other_client:
        theirs = other_client.get(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
        )

    assert mine.status_code == 200
    assert theirs.status_code == 200
    assert len(mine.json()["diagnoses"]) == 3
    assert theirs.json()["diagnoses"] == []


def test_list_diagnoses_does_not_leak_internal_fields(tmp_path: Path) -> None:
    client, store, _ = _client(tmp_path)
    with client:
        client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        )
        response = client.get(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
        )

    body = response.json()
    assert len(body["diagnoses"]) == 1
    item = body["diagnoses"][0]
    # Caller-internal fields are kept off the wire.
    for forbidden in (
        "scope_fingerprint",
        "workspace_id",
        "tenant_id",
        "provider",
        "fixture",
        "evidence",
    ):
        assert forbidden not in item
    # Visible fields are exactly the documented list.
    assert set(item.keys()) == {
        "diagnosis_id",
        "order_no",
        "question",
        "indicator_code",
        "status",
        "created_at",
        "updated_at",
    }


def test_create_diagnosis_requires_question(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1"},
        )

    assert response.status_code == 422


def test_create_diagnosis_rejects_excessive_payload(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "x" * 4001},
        )

    assert response.status_code == 422


def test_create_diagnosis_requires_diagnosis_scope(tmp_path: Path) -> None:
    client, store, runtime = _client(tmp_path, resolver=_Resolver())
    with client:
        response = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer orders-only"},
            json={"order_no": "O-1", "question": "why"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert store.count_standard_diagnoses() == 0
    assert runtime.started == []
