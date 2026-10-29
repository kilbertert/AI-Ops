from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiops_diagnostics.knowledge.knowledge_retrieval import (
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


def test_docx_embedded_image_survives_search_and_media_fetch() -> None:
    signer = _signer()
    chunks = normalize_search_response(
        {
            "chunks": [
                {
                    "id": "chunk-1",
                    "document_id": "doc-1",
                    "dataset_id": "kb-a",
                    "docnm_kwd": "用户端.docx",
                    "doc_type_kwd": "text",
                    "content": "充电启动失败界面",
                    "image_id": "kb-a-chunk-1",
                }
            ]
        },
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_ids=("kb-a",),
        media_signer=signer,
    )
    assert len(chunks[0].media) == 1
    resource = chunks[0].media[0].resource
    assert resource.kind == "image"
    fetched = []

    def fetch(grant, range_header=None):
        fetched.append(grant.backend_id)
        return b"\xff\xd8\xff" + b"x" * 20

    result = MediaProxy(signer, fetch).serve_signed(resource.url.rsplit("/", 1)[-1])
    assert fetched == ["kb-a-chunk-1"]
    assert result.status_code == 200
    assert result.headers["Content-Type"] == "image/jpeg"


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


def test_serve_signed_authenticates_by_url_signature_and_honors_liveness() -> None:
    """The /v1/media data plane: URL HMAC is the credential, grant scope still
    re-validated, liveness callback can revoke before TTL expiry."""
    now = datetime(2026, 9, 10, tzinfo=UTC)
    signer = MediaResourceSigner("test-secret", ttl_seconds=60)
    resource = signer.issue(
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_id="kb-a",
        document_id="doc-1",
        chunk_id="chunk-1",
        backend_id="object-1",
        kind="image",
        mime_type="image/png",
        title="拔枪示意.png",
        reference_id="chunk-1",
        now=now,
    )
    proxy = MediaProxy(signer, lambda _grant: b"0123456789")
    signed_id = resource.url.rsplit("/", 1)[-1]

    full = proxy.serve_signed(signed_id, now=now)
    assert full.status_code == 200
    assert full.headers["Content-Type"] == "image/png"
    assert full.body == b"0123456789"

    partial = proxy.serve_signed(signed_id, range_header="bytes=2-5", now=now)
    assert partial.status_code == 206
    assert partial.body == b"2345"
    assert partial.headers["Content-Range"] == "bytes 2-5/10"

    # Bad signature, expired grant, and inactive liveness all 403 uniformly.
    assert proxy.serve_signed("media_badid.000000000000000000000000", now=now).status_code == 403
    assert proxy.serve_signed(signed_id, now=now + timedelta(seconds=61)).status_code == 403
    assert proxy.serve_signed(signed_id, now=now, is_active=lambda _grant: False).status_code == 403

    # A missing blob is 404, distinct from access denial.
    assert (
        MediaProxy(signer, lambda _grant: (_ for _ in ()).throw(FileNotFoundError("gone")))
        .serve_signed(signed_id, now=now)
        .status_code
        == 404
    )


def test_media_proxy_sniffs_actual_mime_and_slices_ignored_upstream_range() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    signer = MediaResourceSigner("test-secret", ttl_seconds=60)
    resource = signer.issue(
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id=None,
        knowledge_base_id="kb-a",
        document_id="doc-1",
        chunk_id=None,
        backend_id="doc-1",
        kind="image",
        mime_type="image/png",
        title="guide.png",
        reference_id="doc-1",
        now=now,
    )
    # JPEG bytes are returned even though the grant was inferred as PNG.
    body = b"\xff\xd8\xff" + b"x" * 20
    proxy = MediaProxy(signer, lambda _grant, range_header=None: body)
    signed_id = resource.url.rsplit("/", 1)[-1]

    full = proxy.serve_signed(signed_id, now=now)
    assert full.status_code == 200
    assert full.headers["Content-Type"] == "image/jpeg"

    partial = proxy.serve_signed(signed_id, range_header="bytes=2-5", now=now)
    assert partial.status_code == 206
    assert partial.body == body[2:6]
    assert partial.headers["Content-Range"] == "bytes 2-5/23"

    bad = proxy.serve_signed(signed_id, range_header="bytes=999-", now=now)
    assert bad.status_code == 416


# ── 外部播放链接（#1035）────────────────────────────────────────────────


def _normalize(items, signer):
    return normalize_search_response(
        {"chunks": items},
        tenant_id="tenant-a",
        agent_version="agent-v1",
        session_id="conversation-1",
        knowledge_base_ids=("kb-a",),
        media_signer=signer,
    )


def test_a_text_chunk_that_links_a_published_video_becomes_a_video_block() -> None:
    """本仓素材的真实形状：一张「视频标题 + YouTube 链接」的表，解析出来是**纯文本**。

    这条分支在 #1035 之前不存在 —— 文本 chunk 拿不到任何 media，所以那 19 条链接
    永远到不了前端。
    """
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_id": "doc-1",
                "docnm_kwd": "TRENDPOWER_video_modules.md",
                "doc_type_kwd": [],
                "content": (
                    "- 视频宣传标题：0% Tariff! Why Nigerians Are Rushing to Buy Chinese EVs\n"
                    "- YouTube 视频链接：https://www.youtube.com/shorts/Ld7_TVdVFpk\n"
                ),
                "similarity": 0.86,
            }
        ],
        _signer(),
    )

    assert len(chunks) == 1
    media = chunks[0].media[0].resource
    assert media.kind == "video"
    # 外部链接**就写在 url 里**：前端 `parseYoutubeUrl(media.url)` 认得出它，
    # 不需要第二个字段（ADR-0120）。
    assert media.url == "https://www.youtube.com/shorts/Ld7_TVdVFpk"
    assert media.to_dict()["url"] == media.url


def test_a_video_document_without_an_external_link_keeps_its_own_url() -> None:
    """「没有外链」是常态，不是缺陷：我们托管的 mp4 一个字段都不变。"""
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-2",
                "doc_id": "doc-2",
                "docnm_kwd": "新加坡无人电动巴士.mp4",
                "doc_type_kwd": "video",
                "content": "宣传片介绍了新加坡无人电动巴士项目。",
                "similarity": 0.7,
            }
        ],
        _signer(),
    )

    media = chunks[0].media[0].resource
    assert media.url.startswith("/v1/media/media_")
    assert media.to_dict()["url"] == media.url


def test_a_link_on_a_host_outside_the_allow_list_is_silently_ignored() -> None:
    """非白名单域不签发、也不报错 —— 判据是「没有这个字段」。"""
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-3",
                "doc_id": "doc-3",
                "docnm_kwd": "notes.md",
                "content": "参考 https://example.com/video.mp4 和 https://vimeo.com/12345",
            }
        ],
        _signer(),
    )

    assert chunks[0].media == (), "非白名单域不该签发任何 media"


def test_a_lookalike_host_does_not_pass_the_allow_list() -> None:
    """`youtube.com.evil.example` 必须被拒。

    这是前缀匹配与解析后比 host 的差别，也是这条 guard 存在的理由本身。
    """
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-4",
                "doc_id": "doc-4",
                "docnm_kwd": "notes.md",
                "content": "https://youtube.com.evil.example/watch?v=abc",
            }
        ],
        _signer(),
    )

    assert chunks[0].media == (), "形似域名被当成了白名单域"


def test_the_first_allow_listed_link_wins_when_a_chunk_quotes_several() -> None:
    """一个 video 块只对应一个播放源；多条时取第一条，不全都签出去。"""
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-5",
                "doc_id": "doc-5",
                "docnm_kwd": "notes.md",
                "content": ("首个：https://www.youtube.com/watch?v=first\n其次：https://youtu.be/second\n"),
            }
        ],
        _signer(),
    )

    assert len(chunks[0].media) == 1
    assert chunks[0].media[0].resource.url == "https://www.youtube.com/watch?v=first"


def test_a_channel_page_on_an_allow_listed_host_is_not_a_video() -> None:
    """真机抓到的缺陷（2026-10-23，41）：频道主页被签成了 video 块。

    同一个文档里既有 `watch?v=…`（一个视频）也有 `@trendpowercharge`（频道主页）。
    后者签成 video 会让前端把一个播放器挂在一个**没有单个视频可播**的页面上，
    用户看到的是一个坏掉的框 —— 而且看起来像我们的 bug。

    判据：白名单是**host + path** 两级，不是只有 host。
    """
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-ch",
                "doc_id": "doc-ch",
                "docnm_kwd": "video_modules.md",
                "content": "- 官方频道主页 / 检索源：https://www.youtube.com/@trendpowercharge",
            }
        ],
        _signer(),
    )

    assert chunks[0].media == (), "频道主页被当成了单个视频"


def test_a_real_video_url_still_passes_the_path_rule() -> None:
    """收紧 path 判据不能把真视频一起拒掉 —— 三种常见形态各验一次。"""
    for url in (
        "https://www.youtube.com/watch?v=6Ww7A5qZgaA",
        "https://www.youtube.com/shorts/Ld7_TVdVFpk",
        "https://youtu.be/1Lk9eNUuMIV",
        "https://www.youtube.com/live/abc123",
    ):
        chunks = _normalize(
            [
                {
                    "dataset_id": "kb-a",
                    "chunk_id": "chunk-v",
                    "doc_id": "doc-v",
                    "docnm_kwd": "video_modules.md",
                    "content": f"- YouTube 视频链接：{url}",
                }
            ],
            _signer(),
        )
        assert chunks[0].media, f"{url} 应当被签发"
        assert chunks[0].media[0].resource.url == url


def test_a_channel_link_does_not_mask_a_real_video_later_in_the_chunk() -> None:
    """频道链接在前、真视频在后时，要跳过频道那条继续找。

    这条防的是"第一个白名单域链接就用"的写法：它会在这种 chunk 上签出频道页，
    或者干脆什么也不签。
    """
    chunks = _normalize(
        [
            {
                "dataset_id": "kb-a",
                "chunk_id": "chunk-mix",
                "doc_id": "doc-mix",
                "docnm_kwd": "video_modules.md",
                "content": (
                    "- 官方频道主页：https://www.youtube.com/@trendpowercharge\n"
                    "- 视频链接：https://www.youtube.com/watch?v=rH2fOOUpohM\n"
                ),
            }
        ],
        _signer(),
    )

    assert chunks[0].media, "后面的真视频被频道链接挡住了"
    assert chunks[0].media[0].resource.url == "https://www.youtube.com/watch?v=rH2fOOUpohM"


def test_an_external_video_url_looks_like_what_the_frontend_already_parses() -> None:
    """签发出来的 url 必须能被前端的 `parseYoutubeUrl` 认出来。

    这条不是"我们觉得前端能认"，而是照 APK `app-service.js` 里那份实现的实际
    判据复刻的：去掉 `www.` 后必须是 `youtube.com` / `youtu.be` /
    `youtube-nocookie.com`，且 `watch` 取 `v=`、`shorts|embed|live` 取下一段、
    `youtu.be` 取第一段。

    **前端零改动**是这个方案成立的唯一前提（ADR-0120）。它一旦不成立，
    `media.url` 里的外链就会变成一个播不了的原生 `<video>`。
    """
    import urllib.parse

    def frontend_parses(url: str) -> str:
        """复刻 APK 里 parseYoutubeUrl 的 host/path 判据，返回 videoId。"""
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"youtu.be", "youtube.com", "youtube-nocookie.com"} and not (
            host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")
        ):
            return ""
        segments = [s for s in (parts.path or "").split("/") if s]
        if host == "youtu.be":
            return segments[0] if segments else ""
        if segments and segments[0] == "watch":
            return urllib.parse.parse_qs(parts.query).get("v", [""])[0]
        if segments and segments[0] in {"embed", "shorts", "live", "v", "e"}:
            return segments[1] if len(segments) > 1 else ""
        return ""

    for chunk_text, expected in (
        ("https://www.youtube.com/shorts/Ld7_TVdVFpk", "Ld7_TVdVFpk"),
        ("https://www.youtube.com/watch?v=6Ww7A5qZgaA", "6Ww7A5qZgaA"),
        ("https://youtu.be/1Lk9eNUuMIV", "1Lk9eNUuMIV"),
    ):
        chunks = _normalize(
            [
                {
                    "dataset_id": "kb-a",
                    "chunk_id": "chunk-f",
                    "doc_id": "doc-f",
                    "docnm_kwd": "video_modules.md",
                    "content": f"- YouTube 视频链接：{chunk_text}",
                }
            ],
            _signer(),
        )
        url = chunks[0].media[0].resource.to_dict()["url"]
        assert frontend_parses(url) == expected, f"前端认不出 {url}"
