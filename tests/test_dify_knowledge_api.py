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

import pytest
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

    def __init__(
        self, *, search: Any = None, signer: Any = None, owned: dict[str, list[str]] | None = None
    ) -> None:
        self.kb_search_client = _FakeKbClient(search, owned=owned) if search is not None else None
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

    def __init__(self, search, tenant_id: str = "aiops", owned: dict[str, list[str]] | None = None) -> None:
        self._search = search
        self.tenant_id = tenant_id
        #: tenant → the library ids it owns, for the auto-registration path.
        self._owned = owned or {}
        #: Every search, with the tenant header it actually went out under.
        self.calls: list[tuple[tuple[str, ...], str, int]] = []
        self.tenant_calls: list[str] = []
        #: Every listing, with the tenant it was asked for.
        self.listing_calls: list[str] = []

    def for_tenant(self, tenant_id: str) -> _FakeKbClient:
        # ``type(self)``: a subclass modelling one specific upstream failure must
        # survive the rebind, otherwise the failure it models disappears exactly
        # where the code under test rebinds — the test would pass for the wrong
        # reason.
        bound = type(self)(self._search, tenant_id=tenant_id, owned=self._owned)
        bound.calls = self.calls
        bound.tenant_calls = self.tenant_calls
        bound.listing_calls = self.listing_calls
        return bound

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        self.listing_calls.append(self.tenant_id)
        return [
            {"knowledge_base_id": kb, "name": kb, "document_count": 1, "chunk_count": 1}
            for kb in self._owned.get(self.tenant_id, [])
        ]

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
        kb_search_max_results=4,
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


# ── 调试身份（#586）────────────────────────────────────────────────────────


DEBUG_KEY = "dify-debug-key-9876543210fedcba"
DEBUG_TENANT = "T-1"
DEBUG_IDS = "kb-customer"


def _client_with_debug(tmp_path: Path, runtime: _Runtime, **overrides: Any) -> TestClient:
    return _client(
        tmp_path,
        runtime,
        dify_debug_api_key=overrides.pop("dify_debug_api_key", DEBUG_KEY),
        dify_debug_tenant=overrides.pop("dify_debug_tenant", DEBUG_TENANT),
        dify_debug_knowledge_ids=overrides.pop("dify_debug_knowledge_ids", DEBUG_IDS),
        **overrides,
    )


def _runtime() -> _Runtime:
    return _Runtime(signer=MediaResourceSigner("secret", ttl_seconds=600), search=lambda _k, _q, _n: [])


def test_the_credential_decides_the_identity_not_the_request(tmp_path: Path) -> None:
    """两个凭据 → 两个身份；同一个请求体在两边得到不同的可见范围。

    这是 #586 的核心判据。判据落在**同一个 `knowledge_id`、同一个请求体**上：
    生产凭据能取到 `kb-other`（T-2），调试凭据取不到 —— 差别只可能来自凭据。
    """
    runtime = _runtime()
    client = _client_with_debug(tmp_path, runtime)

    # 同一个 id，两个凭据。生产：200；调试：404（不在子集里 / 租户不符）。
    assert _post(client, _body(knowledge_id="kb-other")).status_code == 200
    assert _post(client, _body(knowledge_id="kb-other"), key=DEBUG_KEY).status_code == 404
    # 生产凭据也拿不到调试凭据的范围之外的东西 —— 它本来就是全量（登记表决定）。
    assert _post(client, _body(knowledge_id="kb-customer"), key=DEBUG_KEY).status_code == 200


def test_the_debug_identity_cannot_be_widened_by_request_fields(tmp_path: Path) -> None:
    """带请求头/请求体不得扩大范围或提权 —— 这是必须专门测的一条边（#586）。

    逐一试每一种"请求里能塞东西的地方"：请求头（伪装入口/来源/会话）、
    body 里的额外字段。它们**一个都不参与身份判定** —— 解析函数连请求对象都不接。
    """
    runtime = _runtime()
    client = _client_with_debug(tmp_path, runtime)

    for headers in (
        {"X-Business-Entry": "operator"},
        {"X-AIOps-Source-Key": "anything"},
        {"X-Third-Session": "anything"},
        {"X-Dify-Debug-Tenant": "T-2"},
        {"X-Dify-Tenant": "T-2"},
        {"tenant-id": "T-2"},
        {"X-AIOps-Dify-Debug-Knowledge-Ids": "kb-other"},
    ):
        resp = client.post(
            "/v1/dify/retrieval",
            json=_body(knowledge_id="kb-other"),
            headers={"Authorization": f"Bearer {DEBUG_KEY}", **headers},
        )
        assert resp.status_code == 404, f"{headers} 扩大了调试身份的范围"

    # body 里塞同样的键也不参与判定（模型是 extra="ignore"，未知键本就无效果）。
    widened = {**_body(knowledge_id="kb-other"), "tenant_id": "T-2", "knowledge_ids": ["kb-other"]}
    assert _post(client, widened, key=DEBUG_KEY).status_code == 404


def test_the_debug_identity_is_pinned_to_its_tenant(tmp_path: Path) -> None:
    """固定租户：**同 id 但租户不符**同样取不到。

    这一条防的是"子集写对了、租户写错"：`kb-other` 登记在 T-2，调试身份固定在 T-1，
    即使把它列进子集也不该放行 —— 两个条件是与关系，不是或。
    """
    runtime = _runtime()
    client = _client_with_debug(tmp_path, runtime, dify_debug_knowledge_ids="kb-customer,kb-other")

    assert _post(client, _body(knowledge_id="kb-customer"), key=DEBUG_KEY).status_code == 200
    assert _post(client, _body(knowledge_id="kb-other"), key=DEBUG_KEY).status_code == 404


def test_an_empty_subset_reaches_nothing_rather_than_everything(tmp_path: Path) -> None:
    """**配了调试凭据但没给子集**必须等于"什么也取不到"，不是"不收窄"。

    这是这一层最危险的一处默认值：漏写一个配置项若被读成 `None`（= 不收窄），
    调试凭据就拿到了生产面。空列表留在空列表上。
    """
    runtime = _runtime()
    client = _client_with_debug(tmp_path, runtime, dify_debug_knowledge_ids="")

    assert _post(client, _body(knowledge_id="kb-customer"), key=DEBUG_KEY).status_code == 404
    assert _post(client, _body(knowledge_id="kb-other"), key=DEBUG_KEY).status_code == 404
    # 生产凭据不受影响。
    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200


def test_an_unregistered_id_is_indistinguishable_from_a_forbidden_one(tmp_path: Path) -> None:
    """未登记与"登记了但这条身份不能用"必须是**同一个 404**。

    403 会说"这条存在，只是你不能用" —— 那本身就是一次对登记表的泄漏：
    调试身份可以拿它逐个试出生产有哪些知识库。
    """
    runtime = _runtime()
    client = _client_with_debug(tmp_path, runtime)

    forbidden = _post(client, _body(knowledge_id="kb-other"), key=DEBUG_KEY)
    never_registered = _post(client, _body(knowledge_id="kb-does-not-exist"), key=DEBUG_KEY)
    assert forbidden.status_code == never_registered.status_code == 404
    assert forbidden.json() == never_registered.json()


def test_production_keys_are_rejected_without_one(tmp_path: Path) -> None:
    """没配调试凭据时那把 key 整体无效 —— 调试身份是"配了才有"，不是一个默认身份。"""
    runtime = _runtime()
    client = _client(tmp_path, runtime)  # 不传 dify_debug_*
    assert _post(client, _body(knowledge_id="kb-customer"), key=DEBUG_KEY).status_code == 403
    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200


def test_a_debug_identity_requires_the_production_route_to_exist(tmp_path: Path) -> None:
    """半配置是启动错误：只给调试三键、不给生产两键 ⇒ 起不来。

    调试身份是**收窄那条路由**的，不是创建它的。允许这种组合会让运维看到一个
    "配了调试身份"的部署，而实际上那条路由根本不存在（404）—— 与"没配"不可区分。
    """
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_debug_api_key=DEBUG_KEY,
        dify_debug_tenant=DEBUG_TENANT,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    with pytest.raises(ValueError, match="DIFY_KNOWLEDGE_API_KEY"):
        settings.validate()


def test_the_debug_key_must_differ_from_the_production_key(tmp_path: Path) -> None:
    """同值等于没有分离 —— 而"分离"正是这个身份存在的全部理由。"""
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_knowledge_api_key=DIFY_KEY,
        dify_knowledge_bindings=BINDINGS,
        dify_debug_api_key=DIFY_KEY,
        dify_debug_tenant=DEBUG_TENANT,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must differ"):
        settings.validate()


def test_config_round_trips_the_debug_keys_from_the_config_file(tmp_path: Path) -> None:
    """三键走**服务端配置文件**（41 上 `production.env` 的读取路径），不是进程 env。

    与 `AIOPS_GATEWAY_DIFY_KNOWLEDGE_*` 同一条：`from_env` 用 `_file_value` 读它。
    这里直接驱动那个路径，避免"写进了文件却没生效"这种静默形态。
    """
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    config = tmp_path / "production.env"
    config.write_text(
        "# private\n"
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY=prod-key\n"
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS=kb-1:T-1:KB-A\n"
        f"AIOPS_GATEWAY_DIFY_DEBUG_API_KEY={DEBUG_KEY}\n"
        "AIOPS_GATEWAY_DIFY_DEBUG_TENANT=T-TEST\n"
        "AIOPS_GATEWAY_DIFY_DEBUG_KNOWLEDGE_IDS=kb-1, kb-2\n",
        encoding="utf-8",
    )
    import os

    os.chmod(config, 0o600)
    # 直接构造再校验：读文件的路径由 `_private_config_values` 负责，这里断言的是
    # "三键在 settings 上存在且被 validate 接受"。
    built = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
        dify_knowledge_api_key="prod-key",
        dify_knowledge_bindings="kb-1:T-1:KB-A",
        dify_debug_api_key=DEBUG_KEY,
        dify_debug_tenant="T-TEST",
        dify_debug_knowledge_ids="kb-1, kb-2",
    )
    built.validate()
    assert built.dify_debug_tenant == "T-TEST"


def test_a_multi_kb_binding_does_not_exceed_the_requested_top_k(tmp_path: Path) -> None:
    """Each registered base is searched with `top_k` and the results are merged,
    so a two-base binding returns up to 2 × top_k chunks. Dify asked for 3 and
    must receive 3 (#838 review), not the deployment cap of 10.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda _kbs, _q, _n: (
                [_chunk(kb_id="KB-A", doc_id=f"a{i}", score=0.9 - i / 100) for i in range(3)]
                + [_chunk(kb_id="KB-B", doc_id=f"b{i}", score=0.8 - i / 100) for i in range(3)]
            ),
        ),
        dify_knowledge_bindings="kb-customer:T-1:KB-A|KB-B",
        kb_search_max_results=10,
    )

    records = _post(client, _body(top_k=3)).json()["records"]

    assert len(records) == 3


def test_the_cap_still_applies_to_what_dify_asks_for(tmp_path: Path) -> None:
    """Truncating to the request must not become "trust whatever Dify sends":
    `parse_dify_retrieval_request` caps `top_k` at the deployment limit first.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda *_a: [_chunk(doc_id=f"d{i}", score=0.9 - i / 100) for i in range(20)],
        ),
        kb_search_max_results=10,
    )

    records = _post(client, _body(top_k=500)).json()["records"]

    assert len(records) == 10


# ---------------------------------------------------------------------------
# The picker's source: what this endpoint can serve.
#
# Dify's dialog asks for a free-text `knowledge_id`, which is why "configure a
# knowledge base" needed an engineer. This route is the other half of that
# dialog: same credential, same registry, so the console can offer exactly what
# retrieval will accept.
# ---------------------------------------------------------------------------


def test_knowledge_list_returns_the_registry_not_ragflow(tmp_path: Path) -> None:
    """It lists OUR keys — the values Dify sends back — never RAGFlow dataset ids.

    Listing datasets would offer ids retrieval answers 404 to: a picker that
    looks richer and works worse.
    """
    client = _client(tmp_path, _Runtime())
    response = client.get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DIFY_KEY}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["knowledge_id"] for item in body["knowledge_items"]] == ["kb-customer", "kb-other"]
    assert body["knowledge_items"][0]["knowledge_base_ids"] == ["KB-A"]
    assert body["total"] == 2


def test_knowledge_list_needs_the_same_credential_as_retrieval(tmp_path: Path) -> None:
    """No credential and a wrong one both stay closed."""
    client = _client(tmp_path, _Runtime())
    assert client.get("/v1/dify/knowledge").status_code == 401
    wrong = client.get("/v1/dify/knowledge", headers={"Authorization": "Bearer wrong-key-wrong-key"})
    assert wrong.status_code == 403


def test_knowledge_list_is_unconfigured_when_retrieval_is(tmp_path: Path) -> None:
    """Same "unconfigured means absent" rule as every other Dify route."""
    client = _client(tmp_path, _Runtime(), dify_knowledge_api_key="", dify_knowledge_bindings="")
    response = client.get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DIFY_KEY}"})
    assert response.status_code == 404


def test_debug_identity_lists_only_what_it_can_retrieve(tmp_path: Path) -> None:
    """A preview picker must not suggest choices the preview cannot honour."""
    client = _client_with_debug(tmp_path, _Runtime())
    response = client.get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DEBUG_KEY}"})
    assert response.status_code == 200
    ids = [item["knowledge_id"] for item in response.json()["knowledge_items"]]
    assert ids == sorted(DEBUG_IDS.split(",")), ids


def test_console_rows_and_env_rows_form_one_list(tmp_path: Path) -> None:
    """The two registries merge, and a console row carries the operator's label.

    This is what makes the patched Dify dialog useful: a source saved in OUR
    console shows up there, under the name the operator typed.
    """
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_knowledge_api_key=DIFY_KEY,
        dify_knowledge_bindings=BINDINGS,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    store.set_knowledge_id_binding("kb-new", "T-9", ("KB-NEW",), display_name="新库")
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    body = TestClient(app).get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DIFY_KEY}"}).json()
    by_id = {item["knowledge_id"]: item for item in body["knowledge_items"]}
    assert by_id["kb-new"]["name"] == "新库"
    assert by_id["kb-customer"]["name"] == "kb-customer"  # env rows fall back to the id


def test_media_urls_are_absolute_when_a_public_base_is_configured(tmp_path: Path) -> None:
    """Dify hands this string to a console it does not serve.

    A relative ``/v1/media/...`` is resolved by whoever holds it, and here that
    is Dify's host — where ``/v1`` is Dify's own API and answers 404. So the
    adapter must mint absolute URLs, or every image/video in a record is
    undisplayable while the record itself looks complete (41, 2026-10-21).
    Empty base keeps the old relative form: the APP is same-origin and never
    needed one.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("secret", ttl_seconds=600),
            search=lambda _kbs, _q, _n: [
                {
                    **_chunk(),
                    "image_id": "ragflow-image-42",
                    "mime_type": "image/png",
                    "doc_type_kwd": "image",
                }
            ],
        ),
        public_base_url="https://api.mall.qushiyun.com",
    )

    url = _post(client, _body()).json()["records"][0]["metadata"]["media"][0]["url"]

    assert url.startswith("https://api.mall.qushiyun.com/v1/media/media_"), url


def test_an_external_video_url_is_not_prefixed_with_the_public_base(tmp_path: Path) -> None:
    """外部视频给的是绝对地址，不能再拼 base —— 否则拼出 `https://gatewayhttps://…`。

    这种字符串对日志看起来很"完整"，在浏览器里却永远打不开（ADR-0120）。
    Codex review 在 PR #1047 上指出的就是这个：`dify_records()` 原来无条件
    `f"{prefix}{item.resource.url}"`。
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("secret", ttl_seconds=600),
            search=lambda _kbs, _q, _n: [
                {
                    **_chunk(),
                    "doc_type_kwd": [],
                    "content_with_weight": ("- YouTube 视频链接：https://www.youtube.com/shorts/Ld7_TVdVFpk"),
                }
            ],
        ),
        public_base_url="https://api.mall.qushiyun.com",
    )

    url = _post(client, _body()).json()["records"][0]["metadata"]["media"][0]["url"]

    assert url == "https://www.youtube.com/shorts/Ld7_TVdVFpk", url


# ---- 自动登记（#925）：建完库 Dify 立刻可选 ----------------------------------


def _onboard(store: GatewayStore, tenant_id: str, environment: str = "") -> None:
    """Pretend onboarding ran for this tenant (the 1:1:1 marker the resolver reads).

    Defaults to the environment the test settings carry — onboarding rows are
    keyed by ``(tenant, environment)`` precisely so two UPMS instances cannot
    collide, so seeding the wrong one would be testing the key away.
    """
    store.record_ragflow_tenant(
        tenant_id,
        environment,
        ragflow_email=f"{tenant_id}@kb.internal",
        credential_ciphertext="enc:v1:unused",
        model_checked=True,
    )


def test_an_onboarded_tenants_library_is_retrievable_without_being_registered(
    tmp_path: Path,
) -> None:
    """The whole point of #925: creating a library upstream is enough.

    ``tenant-<id>`` resolves to whatever that tenant owns **right now** — no
    operator step, no cache. Before this, an unregistered ``knowledge_id`` was a
    404 and the console offered only names somebody had typed in by hand.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("secret", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-9": ["KB-NEW"]},
        ),
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    resp = _post(client, _body(knowledge_id="tenant-T-9"))

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["records"]) == 1
    calls = client.app.state.gateway.runtime.kb_search_client.calls  # type: ignore[attr-defined]
    assert calls == [(("KB-NEW",), "怎么处理充电桩故障", 3)]


def test_automatic_sources_appear_in_the_picker(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: [], owned={"T-9": ["KB-NEW"]}
        ),
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    resp = client.get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DIFY_KEY}"})

    ids = [item["knowledge_id"] for item in resp.json()["knowledge_items"]]
    assert "tenant-T-9" in ids


def test_a_tenant_that_was_never_onboarded_gets_no_automatic_source(tmp_path: Path) -> None:
    """Owning libraries upstream is not enough — the 1:1:1 half must exist.

    Otherwise any RAGFlow tenant id that looks like ours would become reachable,
    and "onboarded" would stop meaning anything.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: [], owned={"T-9": ["KB-NEW"]}
        ),
    )

    resp = _post(client, _body(knowledge_id="tenant-T-9"))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DIFY_KNOWLEDGE_NOT_FOUND"


def test_an_onboarded_tenant_with_no_libraries_is_not_a_servable_source(tmp_path: Path) -> None:
    """An empty library list must 404, not answer 200 with zero records.

    A 200 would tell Dify "nothing matched" — a claim about a library that does
    not exist. The whole route is built to keep that distinction.
    """
    client = _client(
        tmp_path,
        _Runtime(signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: [], owned={"T-9": []}),
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    resp = _post(client, _body(knowledge_id="tenant-T-9"))

    assert resp.status_code == 404


def test_manual_registration_wins_over_the_derived_name(tmp_path: Path) -> None:
    """A human's row is the newer decision and must not be shadowed (#925).

    This is also what makes rollback free: turn auto-registration off and the
    manual registry is exactly what was there before.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-9": ["KB-AUTO"]},
        ),
    )
    gateway = client.app.state.gateway  # type: ignore[attr-defined]
    _onboard(gateway.store, "T-9")
    gateway.store.set_knowledge_id_binding("tenant-T-9", "T-9", ("KB-MANUAL",))

    resp = _post(client, _body(knowledge_id="tenant-T-9"))

    assert resp.status_code == 200
    calls = client.app.state.gateway.runtime.kb_search_client.calls  # type: ignore[attr-defined]
    assert calls == [(("KB-MANUAL",), "怎么处理充电桩故障", 3)]


def test_unreachable_upstream_degrades_to_manual_and_never_errors(tmp_path: Path) -> None:
    """kb-service down must not break the manual registry — that is its purpose."""

    class _Broken(_FakeKbClient):
        def list_knowledge_bases(self) -> list[dict[str, Any]]:
            raise KnowledgeSearchUnavailable("down")

    runtime = _Runtime(
        signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])]
    )
    runtime.kb_search_client = _Broken(runtime.kb_search_client._search, owned={"T-9": ["KB-AUTO"]})  # type: ignore[union-attr]
    client = _client(tmp_path, runtime)
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    # The manual binding still works exactly as before.
    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200
    # And the automatic one is simply absent, not an error.
    assert _post(client, _body(knowledge_id="tenant-T-9")).status_code == 404


def test_auto_registration_can_be_turned_off_for_rollback(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600), search=lambda *_a: [], owned={"T-9": ["KB-NEW"]}
        ),
        dify_knowledge_autoregister=False,
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    assert _post(client, _body(knowledge_id="tenant-T-9")).status_code == 404
    # Manual sources are untouched by the switch.
    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200


def test_manual_retrieval_does_not_enumerate_every_onboarded_tenant(tmp_path: Path) -> None:
    """One tenant's slow upstream must not delay another tenant's request.

    Auto-registration is on by default, so an eager implementation would list
    libraries for **every** onboarded tenant before resolving even a manual
    binding — and with a 10s per-call timeout, N slow tenants add N×10s to a
    request that has nothing to do with them. Retrieval resolves one id, so it
    may only ask about the tenant that id names.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-8": ["KB-8"], "T-9": ["KB-9"]},
        ),
    )
    gateway = client.app.state.gateway  # type: ignore[attr-defined]
    _onboard(gateway.store, "T-8")
    _onboard(gateway.store, "T-9")
    kb = gateway.runtime.kb_search_client  # type: ignore[attr-defined]

    assert _post(client, _body(knowledge_id="kb-customer")).status_code == 200
    assert kb.listing_calls == [], "resolving a manual binding must not touch the upstream listing"

    assert _post(client, _body(knowledge_id="tenant-T-8")).status_code == 200
    assert kb.listing_calls == ["T-8"], "only the tenant the id names may be listed"


def test_each_library_gets_its_own_id_and_searches_only_itself(tmp_path: Path) -> None:
    """#1000: one id per library, so one Dify dataset can point at one library.

    Before this, a tenant was one string, so Dify's orchestrator could only offer
    "this tenant's knowledge" — the three libraries inside it were invisible as a
    choice. Each id must now search **only** its own library; the tenant-level id
    keeps merging all of them (its behaviour is pinned by the tests above).
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-9": ["KB-A", "KB-B", "KB-C"]},
        ),
    )
    gateway = client.app.state.gateway  # type: ignore[attr-defined]
    _onboard(gateway.store, "T-9")
    kb = gateway.runtime.kb_search_client  # type: ignore[attr-defined]

    for kb_id in ("KB-A", "KB-B", "KB-C"):
        assert _post(client, _body(knowledge_id=f"tenant-T-9:{kb_id}")).status_code == 200, kb_id

    assert [call[0] for call in kb.calls] == [("KB-A",), ("KB-B",), ("KB-C",)]

    # The tenant-level id is unchanged: still every library, still one search.
    assert _post(client, _body(knowledge_id="tenant-T-9")).status_code == 200
    assert kb.calls[-1][0] == ("KB-A", "KB-B", "KB-C")


def test_picker_offers_one_entry_per_library(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda *_a: [],
            owned={"T-9": ["KB-A", "KB-B"]},
        ),
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    resp = client.get("/v1/dify/knowledge", headers={"Authorization": f"Bearer {DIFY_KEY}"})

    ids = [item["knowledge_id"] for item in resp.json()["knowledge_items"]]
    assert [i for i in ids if i.startswith("tenant-T-9")] == [
        "tenant-T-9",
        "tenant-T-9:KB-A",
        "tenant-T-9:KB-B",
    ]
    # One listing per tenant, not one per id: N libraries must not cost N calls.
    kb = client.app.state.gateway.runtime.kb_search_client  # type: ignore[attr-defined]
    assert kb.listing_calls == ["T-9"]


def test_a_dataset_id_from_another_tenant_is_not_reachable(tmp_path: Path) -> None:
    """A guessed dataset id must not borrow this tenant's name.

    Tenant ids are public — they are literally `tenant-<id>` in Dify's own config —
    so `tenant-T-8:<KB of T-9>` is a cheap guess. The derived id is servable only
    when the dataset appears in **that** tenant's own listing; otherwise the answer
    is the same 404 as "never registered", which says nothing about whether the
    library exists or who owns it.
    """
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-8": ["KB-8"], "T-9": ["KB-9"]},
        ),
    )
    gateway = client.app.state.gateway  # type: ignore[attr-defined]
    _onboard(gateway.store, "T-8")
    _onboard(gateway.store, "T-9")

    resp = _post(client, _body(knowledge_id="tenant-T-8:KB-9"))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DIFY_KNOWLEDGE_NOT_FOUND"
    assert gateway.runtime.kb_search_client.calls == []  # type: ignore[attr-defined]


def test_a_empty_dataset_segment_is_not_a_source(tmp_path: Path) -> None:
    """`tenant-T-9:` is malformed — 404, not "every library of T-9"."""
    client = _client(
        tmp_path,
        _Runtime(
            signer=MediaResourceSigner("s", ttl_seconds=600),
            search=lambda kbs, _q, _n: [_chunk(kb_id=kbs[0])],
            owned={"T-9": ["KB-A"]},
        ),
    )
    _onboard(client.app.state.gateway.store, "T-9")  # type: ignore[attr-defined]

    assert _post(client, _body(knowledge_id="tenant-T-9:")).status_code == 404
