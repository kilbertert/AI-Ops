"""External HTTP seam tests for the standard single-question diagnosis API.

These tests are deliberately written from the outside-in: they build a
``TestClient`` with only the surface that the production wiring exposes
(auth resolver, order authorizer, gateway runtime, gateway store) so that
the implementation can change without breaking the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import DIAGNOSIS_ORDER_OUT_OF_SCOPE
from aiops_diagnostics.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Resolver:
    """Resolves a Bearer token to an immutable ``ScopeContext``.

    Different ``b_user_id`` values produce different ``scope_fingerprint`` values,
    so the test suite can verify cross-caller isolation by switching the
    resolver.
    """

    def __init__(self, b_user_id: str = "B-1") -> None:
        self.b_user_id = b_user_id

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
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
        *,
        conversation_turn=None,
    ) -> dict:
        del conversation_turn
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

    def cancel_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str) -> dict | None:
        """Mirror the runtime's idempotent stop: a terminal row comes back as it
        stands, which is why the cancel body carries `error` for a failed row."""
        diagnosis = self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)
        if diagnosis is None:
            return None
        if diagnosis["status"] in ACTIVE_DIAGNOSIS_STATUSES:
            self.store.update_standard_diagnosis(diagnosis_id, status="cancelled")
            return self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)
        return diagnosis

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
    assert body["language"] == "zh"
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


def test_get_diagnosis_surfaces_the_out_of_scope_code_with_an_unchanged_shape(tmp_path: Path) -> None:
    """The one new externally observable value reaches the wire, and nothing else moves.

    ``DIAGNOSIS_ORDER_OUT_OF_SCOPE`` is the only new observable behavior in this
    change: the frontend must be able to tell an authorization failure from a
    supplier failure. Every other key of the failed-diagnosis response — and
    every existing error code — stays exactly as it was, so this asserts the full
    key set rather than just the code.
    """
    client, store, _ = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        ).json()
        store.update_standard_diagnosis(
            created["diagnosis_id"],
            status="failed",
            error_code=DIAGNOSIS_ORDER_OUT_OF_SCOPE,
            error_message="order is outside the authorized tenant scope",
        )
        response = client.get(
            f"/v1/standard/diagnoses/{created['diagnosis_id']}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert set(body) == {
        "diagnosis_id",
        "order_no",
        "question",
        "indicator_code",
        "language",
        "status",
        "retry_after_ms",
        "result",
        "error",
        "created_at",
        "updated_at",
        "completed_at",
    }
    assert set(body["error"]) == {"code", "message", "retryable"}
    # The CODE is the contract a client branches on, and it is unchanged.
    assert body["error"]["code"] == DIAGNOSIS_ORDER_OUT_OF_SCOPE
    # The MESSAGE is a bounded, localized sentence chosen by that code — NOT
    # the stored `error_message`, which is an engineer's note. This surface
    # already promised it carries no internal run information
    # (standard-api-contract.md §error.message), and it used to return the
    # stored string verbatim.
    assert body["error"]["message"] == "该订单不在当前授权范围内。"
    assert "order is outside the authorized tenant scope" not in body["error"]["message"]
    assert body["result"] is None


# --------------------------------------------------------------------------
# #549: per-request copy follows the request; stored prose reports as stored.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("poll_language", "expected_fragment"),
    [("zh", "本次诊断未能完成"), ("en", "could not be completed"), ("de", "konnte nicht abgeschlossen")],
)
def test_failed_diagnosis_error_message_follows_the_request_language(
    poll_language: str, expected_fragment: str
) -> None:
    """`error.message` is copy the server writes NOW, so it follows the request.

    It used to follow the row's stored language, so polling an `en` diagnosis
    with `Accept-Language: fr` answered in English — the QA surface had the
    right rule and this one did not (#549).
    """
    from aiops_diagnostics.i18n import diagnosis_error_message

    assert expected_fragment in diagnosis_error_message(poll_language, "DIAGNOSIS_FAILED")


def test_stored_result_language_is_reported_as_stored_not_as_requested() -> None:
    """`language` describes the RESULT, so a poller cannot relabel it.

    The prose was generated once, in the language the diagnosis was STARTED in.
    Echoing the poller's language here would claim an English diagnosis is
    French — a false statement about the payload, and the same shape as
    rewriting a successful retrieval into `unavailable`.
    """
    from aiops_diagnostics.gateway_api import _standard_diagnosis_response

    row = {
        "diagnosis_id": "dx_1",
        "order_no": "O-1",
        "question": "q",
        "indicator_code": None,
        "language": "en",  # started in English
        "status": "failed",
        "error_code": "DIAGNOSIS_FAILED",
        "result": None,
        "created_at": "t",
        "updated_at": "t",
        "completed_at": None,
    }
    body = _standard_diagnosis_response(dict(row), "fr")
    # The stored prose language is reported as stored...
    assert body["language"] == "en"
    # ...while the copy the server renders now follows the request.
    assert "abouti" in body["error"]["message"]  # fr
    assert body["error"]["code"] == "DIAGNOSIS_FAILED"
    assert body["error"]["retryable"] is True


def test_two_poll_languages_over_one_row_change_the_message_not_the_result(
    tmp_path: Path,
) -> None:
    """The end-to-end shape of #549, over HTTP.

    One diagnosis row, polled twice with different `Accept-Language`:
    `error.message` follows the request, `language` keeps reporting the stored
    result. This is the assertion the ticket asks for, and it is the one that
    fails if either rule is collapsed into the other.
    """
    client, store, _ = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        ).json()
        store.update_standard_diagnosis(
            created["diagnosis_id"],
            status="failed",
            error_code="DIAGNOSIS_FAILED",
            error_message="internal: provider returned 403",
        )
        url = f"/v1/standard/diagnoses/{created['diagnosis_id']}"
        body = {}
        for lang in ("zh", "de"):
            body[lang] = client.get(
                url,
                headers={"Authorization": "Bearer token", "Accept-Language": lang},
            ).json()

    # The copy the server writes now follows the REQUEST...
    assert body["zh"]["error"]["message"] == "本次诊断未能完成，请稍后重试。"
    assert "konnte nicht abgeschlossen" in body["de"]["error"]["message"]
    assert body["zh"]["error"]["message"] != body["de"]["error"]["message"]
    # ...while the stored prose language is still reported as stored, and the
    # internal reason never appears.
    for lang in ("zh", "de"):
        assert body[lang]["language"] == "zh"  # the row was created with zh
        assert "provider returned 403" not in body[lang]["error"]["message"]
        assert body[lang]["error"]["code"] == "DIAGNOSIS_FAILED"


@pytest.mark.parametrize(
    ("lang", "fragment"),
    [("zh", "本次诊断未能完成"), ("en", "could not be completed"), ("fr", "n'a pas pu aboutir")],
)
def test_cancel_of_a_failed_diagnosis_renders_in_the_request_language(
    lang: str, fragment: str, tmp_path: Path
) -> None:
    """The cancel response carries a failed row's `error.message`, so it is
    subject to the same rule as polling.

    Cancelling is idempotent and non-probing: an already-terminal diagnosis is
    returned as it stands (the runtime's own docstring), which means the cancel
    body includes `error` for a failed row. It got the new language dependency
    in the same change, so it needs its own assertion — a parameter threaded
    through without a test is exactly the "looks wired, was never driven" shape.
    """
    client, store, _ = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1", "question": "test"},
        ).json()
        store.update_standard_diagnosis(
            created["diagnosis_id"],
            status="failed",
            error_code="DIAGNOSIS_FAILED",
            error_message="internal: provider returned 403",
        )
        body = client.post(
            f"/v1/standard/diagnoses/{created['diagnosis_id']}/cancel",
            headers={"Authorization": "Bearer token", "Accept-Language": lang},
        ).json()

    assert body["status"] == "failed"  # terminal row returned as it stands
    assert fragment in body["error"]["message"]
    assert body["language"] == "zh"  # the stored result language, unchanged
    assert "provider returned 403" not in body["error"]["message"]
