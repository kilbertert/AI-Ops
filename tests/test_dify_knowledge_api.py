"""Route-level tests for the Dify knowledge retrieval adapter (#581 / PRD #577).

Driven at the gateway HTTP face — the seam the ticket names — with a real
``create_gateway_app`` and a stub kb-service client, in the style of
``tests/test_media_api.py``. What is pinned here is the Dify External
Knowledge API contract (``records``, and the tenant/allow-list coming from our
registry rather than the request) and the failure answers: unavailable is a
502, never an empty 200.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
    MediaResourceSigner,
)

DIFY_KEY = "dify-key-0123456789abcdef"
BINDINGS = "kb-customer:T-1:KB-A;kb-other:T-2:KB-B"


class _Runtime:
    """Minimal runtime stub: only the two surfaces the adapter reads."""

    def __init__(self, *, search: Any = None, signer: Any = None) -> None:
        self.kb_search_client = _FakeKbClient(search) if search is not None else None
        self.media_signer = signer

    def shutdown(self) -> None:
        pass


class _FakeKbClient:
    """Stand-in for KbServiceClient that records what it was asked for.

    ``tenant_id`` is a real field, not decoration: the adapter must rebind the
    process-level client to the **registered** tenant before searching (#612).
    A stub that ignored the tenant would be blind to exactly that defect — the
    original version of this file was, and the bug shipped.
    """

    def __init__(self, search, tenant_id: str = "aiops") -> None:
        self._search = search
        self.tenant_id = tenant_id
        #: Every search, with the tenant header it actually went out under.
        self.calls: list[tuple[tuple[str, ...], str, int]] = []
        self.tenant_calls: list[str] = []

    def for_tenant(self, tenant_id: str) -> _FakeKbClient:
        bound = _FakeKbClient(self._search, tenant_id=tenant_id)
        bound.calls = self.calls
        bound.tenant_calls = self.tenant_calls
        return bound

    def search(self, knowledge_base_ids: tuple[str, ...], question: str, top_k: int) -> Any:
        self.calls.append((tuple(knowledge_base_ids), question, top_k))
        self.tenant_calls.append(self.tenant_id)
        return self._search(knowledge_base_ids, question, top_k)


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _chunk(
    *,
    kb_id: str = "KB-A",
    doc_id: str = "doc-1",
    content: str = "充电桩故障处理步骤",
    score: float = 0.9,
    title: str = "处理手册",
) -> dict[str, Any]:
    return {
        "knowledge_base_id": kb_id,
        "chunk_id": f"{doc_id}-c1",
        "doc_id": doc_id,
        "content_with_weight": content,
        "similarity": score,
        "docnm_kwd": title,
    }


def _client(tmp_path: Path, runtime: _Runtime, **overrides: Any) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_knowledge_api_key=overrides.pop("dify_knowledge_api_key", DIFY_KEY),
        dify_knowledge_bindings=overrides.pop("dify_knowledge_bindings", BINDINGS),
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def _body(
    *,
    knowledge_id: str = "kb-customer",
    query: str = "怎么处理充电桩故障",
    top_k: int | None = 3,
    score_threshold: float | None = None,
    metadata_condition: Any = None,
) -> dict[str, Any]:
    setting: dict[str, Any] = {}
    if top_k is not None:
        setting["top_k"] = top_k
    if score_threshold is not None:
        setting["score_threshold"] = score_threshold
    return {
        "retrieval_setting": setting,
        "query": query,
        "knowledge_id": knowledge_id,
        "metadata_condition": metadata_condition,
    }


def _post(client: TestClient, body: dict[str, Any], key: str | None = DIFY_KEY):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return client.post("/v1/dify/retrieval", json=body, headers=headers)


def test_records_shape_scope_and_declared_binding(tmp_path: Path) -> None:
    """The whole point: Dify's shape, our scope, and only our declared KBs."""
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    client = _client(
        tmp_path,
        _Runtime(
            signer=signer,
            search=lambda _kbs, _q, _n: [
                _chunk(kb_id="KB-A", doc_id="doc-1", score=0.9, title="处理手册"),
                # A KB this knowledge_id was never registered for. The kb-service
                # stub returns it on purpose: the adapter must drop it, not the
                # upstream.
                _chunk(kb_id="KB-SECRET", doc_id="doc-9", score=0.99, title="别的租户"),
                # No document id -> not renderable, dropped by the normalizer.
                {"knowledge_base_id": "KB-A", "content_with_weight": "无文档标识", "similarity": 0.5},
            ],
        ),
    )

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert set(resp.json()) == {"records"}
    records = resp.json()["records"]
    assert len(records) == 1, records
    record = records[0]
    assert set(record) == {"content", "score", "title", "metadata"}
    assert record["content"] == "充电桩故障处理步骤"
    assert record["score"] == 0.9
    assert record["title"] == "处理手册"
    # Dify reads score/title only when metadata is not None.
    assert record["metadata"] is not None
    assert record["metadata"]["reference_id"] == "doc-1-c1"
    assert record["metadata"]["media"] == []

    # The request carried no tenant and no KB ids at all; the upstream was
    # asked for exactly the registered set.
    calls = client.app.state.gateway.runtime.kb_search_client.calls  # type: ignore[attr-defined]
    assert calls == [(("KB-A",), "怎么处理充电桩故障", 3)]


def test_media_url_is_carried_without_leaking_backend_ids(tmp_path: Path) -> None:
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    client = _client(
        tmp_path,
        _Runtime(
            signer=signer,
            search=lambda _kbs, _q, _n: [
                {
                    **_chunk(),
                    "image_id": "ragflow-image-42",
                    "mime_type": "image/png",
                    "doc_type_kwd": "image",
                }
            ],
        ),
    )

    record = _post(client, _body()).json()["records"][0]

    media = record["metadata"]["media"]
    assert media and media[0]["url"].startswith("/v1/media/media_")
    assert media[0]["mime_type"] == "image/png"
    assert "ragflow-image-42" not in str(record)


def test_empty_hits_are_a_200_with_empty_records(tmp_path: Path) -> None:
    client = _client(
        tmp_path, _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: [])
    )

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json() == {"records": []}


def test_top_k_is_capped_by_our_limit(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
        dify_knowledge_max_top_k=4,
    )

    resp = _post(client, _body(top_k=500))

    assert resp.status_code == 200
    calls = client.app.state.gateway.runtime.kb_search_client.calls  # type: ignore[attr-defined]
    assert calls[0][2] == 4


def test_score_threshold_filters_records(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda *_a: [
                _chunk(doc_id="doc-low", score=0.2),
                _chunk(doc_id="doc-high", score=0.8),
            ],
        ),
    )

    records = _post(client, _body(score_threshold=0.5)).json()["records"]

    assert [item["metadata"]["reference_id"] for item in records] == ["doc-high-c1"]


def test_metadata_condition_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
    )

    resp = _post(client, _body(metadata_condition={"logical_operator": "and", "conditions": []}))

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DIFY_FILTER_UNSUPPORTED"
    assert "records" not in resp.json()
    assert client.app.state.gateway.runtime.kb_search_client.calls == []  # type: ignore[attr-defined]


def test_unknown_knowledge_id_is_404_and_never_reaches_kb_service(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
    )

    resp = _post(client, _body(knowledge_id="kb-not-registered"))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DIFY_KNOWLEDGE_NOT_FOUND"
    assert client.app.state.gateway.runtime.kb_search_client.calls == []  # type: ignore[attr-defined]


def test_missing_and_wrong_credentials_are_rejected_without_calling_kb(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
    )

    missing = _post(client, _body(), key=None)
    wrong = _post(client, _body(), key="not-the-key")
    empty = client.post("/v1/dify/retrieval", json=_body(), headers={"Authorization": "Bearer "})

    assert missing.status_code == 401
    assert wrong.status_code == 403
    assert empty.status_code == 403
    assert client.app.state.gateway.runtime.kb_search_client.calls == []  # type: ignore[attr-defined]


def test_unconfigured_route_is_absent_not_open(tmp_path: Path) -> None:
    """No key configured == the route is not enabled at all (404, no leak)."""
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
        dify_knowledge_api_key="",
        dify_knowledge_bindings="",
    )

    resp = _post(client, _body())

    assert resp.status_code == 404
    assert client.app.state.gateway.runtime.kb_search_client.calls == []  # type: ignore[attr-defined]


def test_upstream_outage_is_502_never_an_empty_200(tmp_path: Path) -> None:
    """'Temporarily unavailable' must never be reported as 'nothing matched'."""

    def _boom(*_args: Any):
        raise KnowledgeSearchUnavailable("kb-service search unavailable")

    client = _client(tmp_path, _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=_boom))

    resp = _post(client, _body())

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "DIFY_KNOWLEDGE_UNAVAILABLE"
    assert resp.json()["error"]["retryable"] is True
    assert "records" not in resp.json()


def test_unconfigured_media_or_kb_plane_is_502_not_empty_records(tmp_path: Path) -> None:
    """The media plane being off is an outage for this route, not an empty hit."""
    client = _client(tmp_path, _Runtime(search=lambda *_a: []))  # no signer

    resp = _post(client, _body())

    assert resp.status_code == 502
    assert "records" not in resp.json()


def test_malformed_body_is_400(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: []),
    )

    for bad in ({}, {"query": "q"}, {"query": "q", "knowledge_id": "kb-customer", "retrieval_setting": 1}):
        resp = _post(client, bad)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "DIFY_INVALID_REQUEST"


def test_the_search_goes_out_under_the_registered_tenant(tmp_path: Path) -> None:
    """The adapter must rebind to the REGISTERED tenant before searching (#612).

    The process-level kb client is bound to the neutral ``aiops`` tenant on
    purpose; every other consumer rebinds first. This one did not, so the search
    went out as ``aiops``, RAGFlow's per-tenant ownership check rejected it
    (``code=102``), and every registered ``knowledge_id`` answered 502 —
    on 41, with a correct credential and a registered id.

    The assertion is on the tenant header **that actually reached the client**,
    not on the answer: that is the observable the bug moved, and the previous
    stub could not see it.
    """
    runtime = _Runtime(signer=MediaResourceSigner("secret", ttl_seconds=600), search=lambda _k, _q, _n: [])
    client = _client(tmp_path, runtime)

    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200

    assert runtime.kb_search_client.tenant_calls == ["T-1"], (
        "检索没有按登记的租户重绑：发出去的是进程级客户端的租户，kb-service 会按它判权并拒掉"
    )


def test_two_registered_ids_go_out_under_their_own_tenants(tmp_path: Path) -> None:
    """Two knowledge_ids bound to two tenants must not share one tenant header.

    A rebind that happened once (or a cached bound client) would look correct on
    a single-tenant test and silently cross tenants as soon as a second binding
    exists.
    """
    runtime = _Runtime(signer=MediaResourceSigner("secret", ttl_seconds=600), search=lambda _k, _q, _n: [])
    client = _client(tmp_path, runtime)

    _post(client, _body(knowledge_id="kb-customer"))  # -> T-1
    _post(client, _body(knowledge_id="kb-other"))  # -> T-2 (see BINDINGS)

    assert runtime.kb_search_client.tenant_calls == ["T-1", "T-2"]


def test_the_client_is_left_bound_to_the_neutral_tenant(tmp_path: Path) -> None:
    """The rebind is per-request: the process-level client must not be mutated.

    Sharing one runtime across tenants means a rebind that wrote through would
    leak the last request's tenant into the next one — the failure mode that
    makes this fix worse than the bug.
    """
    runtime = _Runtime(signer=MediaResourceSigner("secret", ttl_seconds=600), search=lambda _k, _q, _n: [])
    client = _client(tmp_path, runtime)

    _post(client, _body(knowledge_id="kb-customer"))

    assert runtime.kb_search_client.tenant_id == "aiops"
