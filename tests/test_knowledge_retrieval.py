from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchGuard,
    KnowledgeSearchUnavailable,
    MediaProxy,
    MediaResourceSigner,
    RetrievalStatus,
    normalize_search_response,
    parse_byte_range,
)


class _SearchClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response if response is not None else []
        self.error = error
        self.calls: list[tuple[tuple[str, ...], str, int]] = []

    def search(self, knowledge_base_ids: tuple[str, ...], question: str, top_k: int):
        self.calls.append((knowledge_base_ids, question, top_k))
        if self.error:
            raise self.error
        return self.response


def _signer() -> MediaResourceSigner:
    return MediaResourceSigner("test-secret", ttl_seconds=60)


def test_knowledge_search_only_uses_bound_databases_and_caps_calls() -> None:
    client = _SearchClient(
        [
            {
                "knowledge_base_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_id": "doc-1",
                "docnm_kwd": "拔枪示意.png",
                "content_with_weight": "请先停止充电，再按下枪柄卡扣。",
                "image_id": "ragflow-image-1",
                "mime_type": "image/png",
                "score": 0.8,
            },
            {
                "knowledge_base_id": "kb-other",
                "chunk_id": "chunk-x",
                "doc_id": "doc-x",
                "content": "不能越过知识库白名单。",
            },
        ]
    )
    guard = KnowledgeSearchGuard(
        client,
        tenant_id="tenant-a",
        agent_version="agent-v1",
        knowledge_base_ids=["kb-a"],
        media_signer=_signer(),
    )

    result = guard.search("无法拔枪怎么办")

    assert result.status is RetrievalStatus.FOUND
    assert result.calls_used == 1
    assert client.calls == [(("kb-a",), "无法拔枪怎么办", 5)]
    assert len(result.chunks) == 1
    media = result.chunks[0].media[0].resource
    assert media.kind == "image"
    assert media.url.startswith("/v1/media/media_")
    assert "ragflow-image-1" not in media.url

    assert guard.search("再次检索").status is RetrievalStatus.FOUND
    limited = guard.search("超过两次").status
    assert limited is RetrievalStatus.LIMITED
    assert len(client.calls) == 2


def test_video_chunk_gets_playable_resource_and_unavailable_is_explicit() -> None:
    signer = _signer()
    chunks = normalize_search_response(
        {
            "chunks": [
                {
                    "dataset_id": "kb-a",
                    "chunk_id": "chunk-1",
                    "doc_id": "doc-1",
                    "docnm_kwd": "拔枪操作.mp4",
                    "doc_type_kwd": "video",
                    "content": "演示停止充电和拔枪操作。",
                    "similarity": 0.7,
                }
            ]
        },
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_ids=("kb-a",),
        media_signer=signer,
    )

    assert chunks[0].media[0].resource.kind == "video"
    assert chunks[0].media[0].resource.mime_type == "video/mp4"

    invalid = normalize_search_response(
        [
            {
                "knowledge_base_id": "kb-a",
                "chunk_id": "chunk-image",
                "doc_id": "doc-image",
                "docnm_kwd": "not-an-image.bin",
                "content": "附件说明",
                "image_id": "image-1",
                "mime_type": "application/pdf",
            },
            {
                "knowledge_base_id": "kb-a",
                "chunk_id": "chunk-video",
                "doc_id": "doc-video",
                "docnm_kwd": "操作.mp4",
                "content": "视频说明",
                "doc_type_kwd": "video",
                "mime_type": "application/pdf",
            },
        ],
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_ids=("kb-a",),
        media_signer=signer,
    )
    assert all(not chunk.media for chunk in invalid)

    unavailable = KnowledgeSearchGuard(
        _SearchClient(error=KnowledgeSearchUnavailable("down")),
        tenant_id="tenant-a",
        agent_version="agent-v1",
        knowledge_base_ids=["kb-a"],
        media_signer=signer,
    ).search("问题")
    assert unavailable.status is RetrievalStatus.UNAVAILABLE


def test_media_proxy_enforces_scope_expiry_and_byte_ranges() -> None:
    now = datetime(2026, 9, 9, tzinfo=UTC)
    signer = MediaResourceSigner("test-secret", ttl_seconds=60)
    resource = signer.issue(
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_id="kb-a",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="object-1",
        kind="video",
        mime_type="video/mp4",
        title="操作.mp4",
        reference_id="chunk-1",
        now=now,
    )
    proxy = MediaProxy(signer, lambda _grant: b"0123456789")

    full = proxy.serve(
        resource.url,
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        now=now,
    )
    assert full.status_code == 200
    assert full.headers["Content-Type"] == "video/mp4"
    assert full.headers["Accept-Ranges"] == "bytes"
    assert full.body == b"0123456789"

    partial = proxy.serve(
        resource.url,
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        range_header="bytes=2-5",
        now=now,
    )
    assert partial.status_code == 206
    assert partial.body == b"2345"
    assert partial.headers["Content-Range"] == "bytes 2-5/10"

    assert (
        proxy.serve(
            resource.url,
            tenant_id="tenant-b",
            agent_version="agent-v1",
            session_id="conversation-1",
            now=now,
        ).status_code
        == 403
    )
    assert (
        proxy.serve(
            resource.url,
            tenant_id="tenant-a",
            agent_version="agent-v1",
            session_id="conversation-1",
            now=now + timedelta(seconds=61),
        ).status_code
        == 403
    )

    signer.invalidate(resource.resource_id)
    assert (
        proxy.serve(
            resource.url,
            tenant_id="tenant-a",
            agent_version="agent-v1",
            session_id="conversation-1",
            now=now,
        ).status_code
        == 403
    )


def test_byte_range_parser_supports_suffix_and_rejects_multiple_ranges() -> None:
    assert parse_byte_range("bytes=0-", 10) == (0, 9)
    assert parse_byte_range("bytes=-3", 10) == (7, 9)
    try:
        parse_byte_range("bytes=0-1,2-3", 10)
    except ValueError:
        pass
    else:
        raise AssertionError("multiple ranges must be rejected")
