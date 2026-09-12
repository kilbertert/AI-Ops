"""Route-level tests for the /v1/media data plane (T3/#170 follow-up).

Covers the full HTTP wiring: signature-URL auth (no Bearer header), Range
semantics through the route, uniform 404 when the media plane is not
configured, and 503 when the kb-service byte fetch is unavailable.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchUnavailable,
    MediaGrant,
    MediaNotFound,
    MediaProxy,
    MediaResourceSigner,
    MediaResponse,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Caller:
    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        if token == "narrow" and required_scope not in {"aiops:faq:read"}:
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Authorizer:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return True


class _FakeAgentStore:
    """Tenant store whose published selection the liveness check consults."""

    def __init__(self, selection: Any) -> None:
        self.selection = selection

    def list(self, tenant_id: str):
        return []


class _AlwaysValidKnowledge:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        pass


class _Runtime:
    """Minimal runtime stub: only the media surface, wired like production."""

    def __init__(
        self,
        *,
        signer: MediaResourceSigner | None = None,
        fetch: Any = None,
        agent_store: Any = None,
    ) -> None:
        self.media_signer = signer
        self.kb_search_client = None
        self.agent_store = agent_store
        if signer is not None and fetch is not None:
            self.kb_search_client = _FakeKbClient(fetch)
        self._proxy = MediaProxy(signer, fetch) if signer is not None and fetch is not None else None

    def serve_media(self, signed_id: str, *, range_header: str | None = None) -> MediaResponse | None:
        if self._proxy is None:
            return None
        return self._proxy.serve_signed(signed_id, range_header=range_header)

    def shutdown(self) -> None:
        pass


class _FakeKbClient:
    """Stand-in for KbServiceClient that records fetches, tenant-bound."""

    def __init__(self, fetch) -> None:
        self._fetch = fetch
        self.fetched: list[str] = []

    def fetch_media(self, grant: MediaGrant) -> bytes:
        self.fetched.append(grant.backend_id)
        return self._fetch(grant)


def _client(tmp_path: Path, runtime: _Runtime) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=_Authorizer(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def _issue(signer: MediaResourceSigner, *, kind: str = "image", mime: str = "image/png") -> str:
    resource = signer.issue(
        tenant_id="T-1",
        agent_version="agent-1#v1",
        session_id="sess-1",
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="backend-1",
        kind=kind,
        mime_type=mime,
        title="示意.png",
        reference_id="chunk-1",
    )
    return resource.url.rsplit("/", 1)[-1]


def test_media_route_requires_no_auth_header_and_serves_bytes(tmp_path: Path) -> None:
    """The URL signature is the credential — no Bearer header needed."""
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    signed_id = _issue(signer)
    client = _client(tmp_path, _Runtime(signer=signer, fetch=lambda _grant: b"PNGDATA"))

    resp = client.get(f"/v1/media/{signed_id}")  # no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"PNGDATA"
    assert resp.headers["cache-control"] == "private, no-store"


def test_media_route_serves_range_requests_for_video(tmp_path: Path) -> None:
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    signed_id = _issue(signer, kind="video", mime="video/mp4")
    body = bytes(range(10))
    client = _client(tmp_path, _Runtime(signer=signer, fetch=lambda _grant: body))

    resp = client.get(f"/v1/media/{signed_id}", headers={"Range": "bytes=2-5"})
    assert resp.status_code == 206
    assert resp.content == body[2:6]
    assert resp.headers["content-range"] == "bytes 2-5/10"

    bad = client.get(f"/v1/media/{signed_id}", headers={"Range": "bytes=0-1,3-4"})
    assert bad.status_code == 416
    assert bad.headers["content-range"] == "bytes */10"


def test_media_route_rejects_tampered_ids_uniformly(tmp_path: Path) -> None:
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    _issue(signer)
    client = _client(tmp_path, _Runtime(signer=signer, fetch=lambda _grant: b"x"))

    for bad_id in ("media_nothing.000000000000000000000000", "media_nosignature", "plain-junk"):
        resp = client.get(f"/v1/media/{bad_id}")
        assert resp.status_code == 403, bad_id


def test_media_route_answers_404_when_plane_not_configured(tmp_path: Path) -> None:
    """No kb-service/media secret configured → uniform 404, no existence leak."""
    client = _client(tmp_path, _Runtime())
    resp = client.get("/v1/media/media_whatever.deadbeefdeadbeefdeadbeef")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MEDIA_NOT_FOUND"


def test_kb_client_fetch_media_routes_image_and_video_paths(monkeypatch) -> None:
    """Image grants hit the image passthrough; video grants the download path."""
    seen: list[tuple[str, dict[str, str]]] = []

    class _Response:
        def __init__(self) -> None:
            self.status = 200

        def read(self) -> bytes:
            return b"BLOB"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, {k: v for k, v in request.header_items()}))
        return _Response()

    monkeypatch.setattr("aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen", fake_urlopen)
    client = KbServiceClient("http://kb.local", tenant_id="tenant-a", service_token="svc-token")

    image_grant = MediaGrant(
        resource_id="media_img",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="ds1-img-key",
        kind="image",
        mime_type="image/png",
        title=None,
        reference_id="chunk-1",
        expires_at=None,  # type: ignore[arg-type]
    )
    assert client.fetch_media(image_grant) == b"BLOB"
    assert seen[-1][0] == "http://kb.local/kb/documents/images/ds1-img-key"

    video_grant = MediaGrant(
        resource_id="media_vid",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-9",
        chunk_id="chunk-9",
        backend_id="doc-9",
        kind="video",
        mime_type="video/mp4",
        title="视频.mp4",
        reference_id="chunk-9",
        expires_at=None,  # type: ignore[arg-type]
    )
    assert client.fetch_media(video_grant) == b"BLOB"
    assert seen[-1][0] == "http://kb.local/kb/knowledge-bases/kb-1/documents/doc-9/download"
    # Every fetch is tenant-scoped via the header, never via the path.
    assert seen[-1][1]["Tenant-id"] == "tenant-a"
    # Range requests are forwarded upstream so a seek fetches only the
    # requested bytes instead of buffering the whole video.
    client.fetch_media(video_grant, range_header="bytes=2-5")
    assert seen[-1][1]["Range"] == "bytes=2-5"


def test_kb_client_fetch_media_maps_missing_and_unavailable(monkeypatch) -> None:
    client = KbServiceClient("http://kb.local", tenant_id="tenant-a")
    grant = MediaGrant(
        resource_id="media_x",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id=None,
        backend_id="gone-1",
        kind="image",
        mime_type="image/png",
        title=None,
        reference_id="doc-1",
        expires_at=None,  # type: ignore[arg-type]
    )

    def not_found(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen", not_found)
    try:
        client.fetch_media(grant)
    except MediaNotFound:
        pass
    else:
        raise AssertionError("404 must map to MediaNotFound")

    def unreachable(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen", unreachable)
    try:
        client.fetch_media(grant)
    except Exception as exc:  # KnowledgeSearchUnavailable — route maps to 503
        assert type(exc).__name__ == "KnowledgeSearchUnavailable"
    else:
        raise AssertionError("transport failure must not be swallowed")


def test_kb_client_maps_http_200_business_error_body_to_not_found(monkeypatch) -> None:
    """RAGFlow can return document-not-found as HTTP 200 JSON."""
    client = KbServiceClient("http://kb.local", tenant_id="tenant-a")
    grant = MediaGrant(
        resource_id="media_x",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id=None,
        backend_id="doc-1",
        kind="video",
        mime_type="video/mp4",
        title="video.mp4",
        reference_id="doc-1",
        expires_at=None,  # type: ignore[arg-type]
    )

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"code":102,"message":"document not found"}\n'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen",
        lambda request, timeout=None: _Response(),
    )
    try:
        client.fetch_media(grant)
    except MediaNotFound:
        pass
    else:
        raise AssertionError("HTTP 200 business error must map to MediaNotFound")


def test_kb_client_retries_transient_video_not_found(monkeypatch) -> None:
    client = KbServiceClient("http://kb.local", tenant_id="tenant-a")
    grant = MediaGrant(
        resource_id="media_x",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id=None,
        backend_id="doc-1",
        kind="video",
        mime_type="video/mp4",
        title="video.mp4",
        reference_id="doc-1",
        expires_at=None,  # type: ignore[arg-type]
    )
    calls = 0

    class _Response:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self) -> bytes:
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(b'{"code":102,"message":"document not found"}')
        return _Response(b"\x00\x00\x00\x18ftypisom")

    monkeypatch.setattr("aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen", fake_urlopen)
    assert client.fetch_media(grant).startswith(b"\x00\x00\x00\x18ftyp")
    assert calls == 2


def test_kb_client_maps_http_200_unknown_business_error_to_unavailable(monkeypatch) -> None:
    client = KbServiceClient("http://kb.local", tenant_id="tenant-a")
    grant = MediaGrant(
        resource_id="media_x",
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id=None,
        backend_id="doc-1",
        kind="video",
        mime_type="video/mp4",
        title="video.mp4",
        reference_id="doc-1",
        expires_at=None,  # type: ignore[arg-type]
    )

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"code":500,"message":"upstream unavailable"}\n'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "aiops_diagnostics.knowledge_retrieval.urllib.request.urlopen",
        lambda request, timeout=None: _Response(),
    )
    try:
        client.fetch_media(grant)
    except KnowledgeSearchUnavailable:
        pass
    else:
        raise AssertionError("unknown HTTP 200 business error must be unavailable")


def test_runtime_serve_media_rechecks_published_agent_liveness(tmp_path: Path) -> None:
    """The real GatewayRuntime liveness check: an issued URL serves while the
    tenant's customer agent stays published with the same KB binding, and
    turns 403 once the agent is disabled or republished at another version —
    before the 600s TTL runs out (#168 acceptance: stale URLs must fail)."""
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.scope_context import DataScope, SubjectRecord

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    agent_store = AgentStore(settings.database_file)
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    runtime = GatewayRuntime(
        store,
        settings,
        Settings(agent=Settings().agent),  # type: ignore[arg-type]
        kb_search_client=_FakeKbClient(lambda _grant: b"PNGDATA"),
        media_signer=signer,
        agent_store=agent_store,
    )

    admin_subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    admin = ScopeContext.build(
        caller=admin_subject,
        subject=admin_subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )
    manager = AgentManager(
        agent_store,
        allowed_models=("aiops-api",),
        knowledge_resolver=_AlwaysValidKnowledge(),
    )
    agent = manager.create(
        admin,
        name="客服",
        description="客服助手",
        config=AgentConfig(
            agent_type="customer",
            prompt="回答必须引用已授权的业务资料。",
            knowledge_base_ids=("kb-1",),
            model="aiops-api",
            output_contract="blocks-v1",
            opening_questions=("怎么处理？",),
            quick_commands=("查看步骤",),
        ),
    )
    published = manager.publish(admin, agent.agent_id, expected_revision=agent.revision)

    resource = signer.issue(
        tenant_id="tenant-a",
        agent_version=f"{agent.agent_id}#v{published.version_no}",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="backend-1",
        kind="image",
        mime_type="image/png",
        title="示意.png",
        reference_id="chunk-1",
    )
    signed_id = resource.url.rsplit("/", 1)[-1]
    response = runtime.serve_media(signed_id)
    assert response is not None and response.status_code == 200
    assert response.body == b"PNGDATA"

    # Disabling the tenant's customer agent invalidates the URL before TTL.
    current = manager.get(admin, agent.agent_id)
    manager.disable(admin, agent.agent_id, expected_revision=current.revision)
    response = runtime.serve_media(signed_id)
    assert response is not None and response.status_code == 403


def test_media_route_maps_non_ascii_ids_to_uniform_403(tmp_path: Path) -> None:
    """Non-ASCII URL ids must hit the uniform 403, never a 500 (public path).

    Regression for the cross-review finding on #181: `media_中文.sig` used to
    raise UnicodeEncodeError from encode("ascii") before any format check,
    surfacing as a 500 instead of 403 no-store.
    """
    signer = MediaResourceSigner("secret", ttl_seconds=600)
    _issue(signer)
    client = _client(tmp_path, _Runtime(signer=signer, fetch=lambda _grant: b"x"))

    # URL-encoded CJK id ("media_中文.sig") and an oversized id.
    resp = client.get("/v1/media/media_%E4%B8%AD.sig")
    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"

    oversized = "media_" + "a" * 200 + ".deadbeefdeadbeefdeadbeef"
    resp = client.get(f"/v1/media/{oversized}")
    assert resp.status_code == 403



def test_runtime_serve_media_fetches_through_grant_tenant(tmp_path: Path) -> None:
    """Regression (#175 C4 video canary, 2026-09-12): the media fetch must be
    re-bound to the grant's tenant. The process-level kb client is bound to
    the neutral "aiops" tenant; fetching through it made kb-service answer
    {"code":102,"document not found"} for documents owned by real tenants —
    deterministic 404 on every signed video URL while the same doc_id
    downloaded fine under the owning tenant's header."""
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.scope_context import DataScope, SubjectRecord

    class _TenantRecordingClient:
        bound_tenant = "aiops"

        def __init__(self) -> None:
            self.fetches: list[tuple[str, str]] = []  # (client tenant, grant tenant)

        def for_tenant(self, tenant_id: str) -> "_TenantRecordingClient":
            clone = _TenantRecordingClient.__new__(_TenantRecordingClient)
            clone.fetches = self.fetches  # shared record across clones
            clone.bound_tenant = tenant_id
            return clone

        def fetch_media(self, grant: MediaGrant, *, range_header: str | None = None) -> bytes:
            self.fetches.append((self.bound_tenant, grant.tenant_id))
            return b"MP4DATA"

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    agent_store = AgentStore(settings.database_file)
    kb_client = _TenantRecordingClient()
    runtime = GatewayRuntime(
        store,
        settings,
        Settings(agent=Settings().agent),  # type: ignore[arg-type]
        kb_search_client=kb_client,  # type: ignore[arg-type]
        media_signer=MediaResourceSigner("secret", ttl_seconds=600),
        agent_store=agent_store,
    )

    admin_subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-video")
    admin = ScopeContext.build(
        caller=admin_subject,
        subject=admin_subject,
        delegated=False,
        effective_tenant_id="tenant-video",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )
    manager = AgentManager(
        agent_store,
        allowed_models=("aiops-api",),
        knowledge_resolver=_AlwaysValidKnowledge(),
    )
    agent = manager.create(
        admin,
        name="客服",
        description="客服助手",
        config=AgentConfig(
            agent_type="customer",
            prompt="回答必须引用已授权的业务资料。",
            knowledge_base_ids=("kb-1",),
            model="aiops-api",
            output_contract="blocks-v1",
            opening_questions=("怎么处理？",),
            quick_commands=("查看步骤",),
        ),
    )
    published = manager.publish(admin, agent.agent_id, expected_revision=agent.revision)

    resource = runtime.media_signer.issue(  # type: ignore[union-attr]
        tenant_id="tenant-video",
        agent_version=f"{agent.agent_id}#v{published.version_no}",
        session_id=None,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="doc-1",
        kind="video",
        mime_type="video/mp4",
        title="视频.mp4",
        reference_id="chunk-1",
    )
    signed_id = resource.url.rsplit("/", 1)[-1]
    response = runtime.serve_media(signed_id)
    assert response is not None and response.status_code == 200
    assert response.body == b"MP4DATA"
    # The fetch reached kb-service under the grant's tenant, never under the
    # process-level neutral "aiops" binding.
    assert kb_client.fetches == [("tenant-video", "tenant-video")]
