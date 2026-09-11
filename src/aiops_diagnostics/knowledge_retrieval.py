"""Bounded knowledge retrieval and media delivery primitives.

This module deliberately does not know about the assistant classifier or
conversation store.  It gives those higher-level paths one small boundary:
Codex may ask for a query, while the harness owns the tenant, agent version,
knowledge-base allow-list, result limits, and media authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import mimetypes
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from aiops_diagnostics.redaction import redact_text

KNOWLEDGE_SEARCH_TOOL = "knowledge_search"
ALLOWED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
ALLOWED_VIDEO_TYPES = frozenset({"video/mp4", "video/webm"})
ALLOWED_MEDIA_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES


class RetrievalStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"


class KnowledgeSearchUnavailable(RuntimeError):
    """The knowledge-base dependency could not provide a result."""


class MediaAccessDenied(ValueError):
    """A media resource is expired, revoked, or outside the caller scope."""


class MediaNotFound(FileNotFoundError):
    """The authorized media object is no longer available."""


class MediaRangeError(ValueError):
    """A Range header is malformed or cannot be served."""


def _raise_for_media_error_body(body: bytes) -> None:
    """Map RAGFlow's HTTP-200 JSON error envelope to media exceptions."""
    candidate = body.lstrip()
    if not candidate.startswith(b"{"):
        return
    try:
        payload = json.loads(candidate.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(payload, dict) or "code" not in payload:
        return
    code = str(payload.get("code") or "")
    if code in {"", "0"}:
        return
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    if code == "102" or "not found" in message.lower():
        raise MediaNotFound(message or "media not found")
    raise KnowledgeSearchUnavailable("kb-service returned a media error")


def _sniff_media_mime(body: bytes, declared: str) -> str:
    """Prefer the actual media signature over a stale document extension."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if len(body) >= 8 and body[4:8] == b"ftyp":
        return "video/mp4"
    return declared


class KnowledgeSearchClient(Protocol):
    def search(self, knowledge_base_ids: tuple[str, ...], question: str, top_k: int) -> Any: ...


@dataclass(frozen=True, slots=True)
class MediaGrant:
    resource_id: str
    tenant_id: str
    agent_version: str
    session_id: str | None
    knowledge_base_id: str
    document_id: str
    chunk_id: str | None
    backend_id: str
    kind: str
    mime_type: str
    title: str | None
    reference_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MediaResource:
    resource_id: str
    url: str
    kind: str
    mime_type: str
    title: str | None
    reference_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "url": self.url,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "title": self.title,
            "reference_id": self.reference_id,
        }


class MediaResourceSigner:
    """Issue random, short-lived resource IDs and verify their grants.

    Grants are intentionally kept behind this boundary; callers never see
    RAGFlow IDs or object-storage paths.  The in-memory registry is sufficient
    for the single-process gateway; a shared store is the follow-up when the
    media proxy is deployed with multiple workers.
    """

    def __init__(self, secret: str | bytes, *, base_path: str = "/v1/media", ttl_seconds: int = 600):
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("media signing secret is required")
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("media ttl must be between 60 and 3600 seconds")
        if not base_path.startswith("/") or "?" in base_path or "#" in base_path:
            raise ValueError("media base path must be an absolute path")
        self._secret = bytes(secret)
        self._base_path = base_path.rstrip("/")
        self._ttl = ttl_seconds
        self._grants: dict[str, MediaGrant] = {}
        self._invalidated: set[str] = set()

    def issue(
        self,
        *,
        tenant_id: str,
        agent_version: str,
        session_id: str | None,
        knowledge_base_id: str,
        document_id: str,
        chunk_id: str | None,
        backend_id: str,
        kind: str,
        mime_type: str,
        reference_id: str,
        title: str | None = None,
        now: datetime | None = None,
    ) -> MediaResource:
        if kind not in {"image", "video"}:
            raise ValueError("media kind must be image or video")
        if mime_type not in ALLOWED_MEDIA_TYPES:
            raise ValueError("media mime type is not allowed")
        if kind == "image" and mime_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("image mime type is not allowed")
        if kind == "video" and mime_type not in ALLOWED_VIDEO_TYPES:
            raise ValueError("video mime type is not allowed")
        fields = {
            "tenant_id": tenant_id,
            "agent_version": agent_version,
            "session_id": session_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "backend_id": backend_id,
            "kind": kind,
            "mime_type": mime_type,
            "title": title,
            "reference_id": reference_id,
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for key, value in fields.items()
            if key not in {"session_id", "chunk_id", "title"}
        ):
            raise ValueError("media grant identifiers must be non-empty")
        issued_at = _utc(now)
        resource_id = "media_" + secrets.token_urlsafe(24)
        expires_at = issued_at + timedelta(seconds=self._ttl)
        self._grants[resource_id] = MediaGrant(
            resource_id=resource_id,
            expires_at=expires_at,
            **fields,
        )
        signature = hmac.new(self._secret, resource_id.encode("ascii"), hashlib.sha256).hexdigest()[:24]
        return MediaResource(
            resource_id=resource_id,
            url=f"{self._base_path}/{resource_id}.{signature}",
            kind=kind,
            mime_type=mime_type,
            title=title,
            reference_id=reference_id,
        )

    def invalidate(self, resource_id: str) -> None:
        candidate = resource_id.rsplit("/", 1)[-1]
        bare_id = candidate.split(".", 1)[0]
        if not bare_id.startswith("media_"):
            raise MediaAccessDenied("media resource is not available")
        self._invalidated.add(bare_id)

    def verify(
        self,
        resource_id: str,
        *,
        tenant_id: str,
        agent_version: str,
        session_id: str | None = None,
        now: datetime | None = None,
        is_active: Callable[[MediaGrant], bool] | None = None,
    ) -> MediaGrant:
        bare_id, signature = _split_resource_id(resource_id)
        expected = hmac.new(self._secret, bare_id.encode("ascii"), hashlib.sha256).hexdigest()[:24]
        grant = self._grants.get(bare_id)
        current = _utc(now)
        if (
            grant is None
            or not hmac.compare_digest(signature, expected)
            or bare_id in self._invalidated
            or grant.expires_at <= current
            or grant.tenant_id != tenant_id
            or grant.agent_version != agent_version
            or (grant.session_id is not None and grant.session_id != session_id)
            or (is_active is not None and not is_active(grant))
        ):
            raise MediaAccessDenied("media resource is not available")
        return grant

    def verify_signed(
        self,
        signed_id: str,
        *,
        now: datetime | None = None,
        is_active: Callable[[MediaGrant], bool] | None = None,
    ) -> MediaGrant:
        """Verify by URL signature alone; the grant carries its own scope.

        Used by the /v1/media HTTP route where the browser cannot present a
        caller identity — the short-lived HMAC in the URL is the credential.
        Everything except caller-scope comparison is checked exactly as
        ``verify`` does: existence, signature, invalidation, TTL, and the
        optional liveness callback.
        """
        bare_id, signature = _split_resource_id(signed_id)
        expected = hmac.new(self._secret, bare_id.encode("ascii"), hashlib.sha256).hexdigest()[:24]
        grant = self._grants.get(bare_id)
        current = _utc(now)
        if (
            grant is None
            or not hmac.compare_digest(signature, expected)
            or bare_id in self._invalidated
            or grant.expires_at <= current
            or (is_active is not None and not is_active(grant))
        ):
            raise MediaAccessDenied("media resource is not available")
        return grant


@dataclass(frozen=True, slots=True)
class MediaResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class MediaProxy:
    """Serve an authorized media blob with browser-compatible Range support.

    ``serve`` is the direct form (caller already authenticated, scope checked
    by the caller). ``serve_signed`` is the URL-signature form used by the
    ``/v1/media/{resource_id}.{signature}`` HTTP route: browsers load media
    via ``<img>``/``<video>`` tags that cannot attach Bearer headers, so the
    short-lived HMAC in the URL *is* the credential (#168 media protocol).
    Both re-check ``is_active`` so a disabled segment or unpublished agent
    version invalidates URLs before their TTL runs out.
    """

    def __init__(self, signer: MediaResourceSigner, fetch: Callable[[MediaGrant], bytes]):
        self.signer = signer
        self.fetch = fetch

    @staticmethod
    def _response_for(grant: MediaGrant, body: bytes, range_header: str | None) -> MediaResponse:
        """Shared response shaping: 200 full, 206 slice, 416 rejected range."""
        common = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "Content-Type": _sniff_media_mime(body, grant.mime_type),
            "Content-Disposition": "inline",
        }
        if not range_header:
            return MediaResponse(200, {**common, "Content-Length": str(len(body))}, body)
        try:
            start, end = parse_byte_range(range_header, len(body))
        except MediaRangeError:
            return MediaResponse(
                416,
                {**common, "Content-Range": f"bytes */{len(body)}", "Content-Length": "0"},
                b"",
            )
        chunk = body[start : end + 1]
        return MediaResponse(
            206,
            {
                **common,
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{len(body)}",
            },
            chunk,
        )

    def serve(
        self,
        resource_id: str,
        *,
        tenant_id: str,
        agent_version: str,
        session_id: str | None = None,
        range_header: str | None = None,
        is_active: Callable[[MediaGrant], bool] | None = None,
        now: datetime | None = None,
    ) -> MediaResponse:
        try:
            grant = self.signer.verify(
                resource_id,
                tenant_id=tenant_id,
                agent_version=agent_version,
                session_id=session_id,
                now=now,
                is_active=is_active,
            )
            body = self.fetch(grant)
        except MediaAccessDenied:
            return MediaResponse(403, {"Cache-Control": "no-store"}, b"")
        except (MediaNotFound, FileNotFoundError):
            return MediaResponse(404, {"Cache-Control": "no-store"}, b"")
        if not isinstance(body, bytes):
            raise TypeError("media fetcher must return bytes")
        return self._response_for(grant, body, range_header)

    def serve_signed(
        self,
        signed_id: str,
        *,
        range_header: str | None = None,
        is_active: Callable[[MediaGrant], bool] | None = None,
        now: datetime | None = None,
    ) -> MediaResponse:
        """Serve by the URL's HMAC signature alone (no caller scope in play).

        The signature authenticates the URL; the grant itself carries the
        tenant/agent-version/session scope and is re-validated here, including
        the ``is_active`` liveness check. Anything wrong is a uniform 403 —
        the same body the T1 tests expect, and no existence leak.
        """
        try:
            grant = self.signer.verify_signed(signed_id, now=now, is_active=is_active)
            # Range passthrough: a range-aware fetcher (kb-service client)
            # fetches only the requested bytes upstream; plain fetchers keep
            # the whole-object-then-slice behavior. The upstream 206 slice is
            # byte-identical to a local slice of the full object, so the
            # response shaping is the same either way.
            fetched = (
                self.fetch(grant, range_header=range_header)
                if _accepts_range(self.fetch)
                else self.fetch(grant)
            )
            if not isinstance(fetched, bytes):
                raise TypeError("media fetcher must return bytes")
            if _accepts_range(self.fetch):
                # Some kb-service versions ignore Range and return the full
                # object. Detect that shape and slice locally; a range-aware
                # upstream still gets the lighter response path.
                if not range_header:
                    return self._response_for(grant, fetched, None)
                try:
                    start, end = parse_byte_range(range_header, len(fetched))
                except MediaRangeError:
                    return self._response_for(grant, fetched, range_header)
                if len(fetched) != end - start + 1:
                    return self._response_for(grant, fetched, range_header)
                return _ranged_response(grant, fetched, range_header)
            return self._response_for(grant, fetched, range_header)
        except MediaAccessDenied:
            return MediaResponse(403, {"Cache-Control": "no-store"}, b"")
        except (MediaNotFound, FileNotFoundError):
            return MediaResponse(404, {"Cache-Control": "no-store"}, b"")


def _accepts_range(fetch: Callable[..., bytes]) -> bool:
    """Whether a media fetcher supports the ``range_header`` keyword."""
    try:
        return "range_header" in inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False


def _ranged_response(grant: MediaGrant, fetched: bytes, range_header: str | None) -> MediaResponse:
    """Shape the response for a range-passthrough fetch.

    With ``range_header`` the upstream already returned the slice, and its
    ``Content-Range`` total is unknown here — emit 206 with the slice length
    and a total-less Content-Range, which browsers accept alongside the
    upstream Accept-Ranges. Without a range the fetcher returned the whole
    object; plain 200.
    """
    common = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Type": _sniff_media_mime(fetched, grant.mime_type),
        "Content-Disposition": "inline",
    }
    if not range_header:
        return MediaResponse(200, {**common, "Content-Length": str(len(fetched))}, fetched)
    return MediaResponse(
        206,
        {**common, "Content-Length": str(len(fetched))},
        fetched,
    )


@dataclass(frozen=True, slots=True)
class RetrievedMedia:
    resource: MediaResource


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    reference_id: str
    content: str
    title: str | None
    score: float | None
    media: tuple[RetrievedMedia, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "content": self.content,
            "title": self.title,
            "score": self.score,
            "media": [item.resource.to_dict() for item in self.media],
        }


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    status: RetrievalStatus
    chunks: tuple[RetrievedChunk, ...]
    calls_used: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_status": self.status.value,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "calls_used": self.calls_used,
        }


class KnowledgeSearchGuard:
    """The only surface a Codex-facing customer agent should call."""

    def __init__(
        self,
        client: KnowledgeSearchClient,
        *,
        tenant_id: str,
        agent_version: str,
        knowledge_base_ids: Sequence[str],
        media_signer: MediaResourceSigner,
        session_id: str | None = None,
        max_results: int = 5,
        max_calls: int = 2,
    ):
        if not tenant_id.strip() or not agent_version.strip():
            raise ValueError("tenant_id and agent_version are required")
        ids = tuple(dict.fromkeys(item.strip() for item in knowledge_base_ids if item and item.strip()))
        if not ids:
            raise ValueError("at least one knowledge base is required")
        if not 1 <= max_results <= 20 or not 1 <= max_calls <= 2:
            raise ValueError("knowledge search limits are out of range")
        self._client = client
        self._tenant_id = tenant_id
        self._agent_version = agent_version
        self._knowledge_base_ids = ids
        self._media_signer = media_signer
        self._session_id = session_id
        self._max_results = max_results
        self._max_calls = max_calls
        self.calls_used = 0

    @property
    def knowledge_base_ids(self) -> tuple[str, ...]:
        return self._knowledge_base_ids

    def search(self, query: str) -> KnowledgeSearchResult:
        query = (query or "").strip()
        if not query:
            raise ValueError("knowledge search query must not be empty")
        if self.calls_used >= self._max_calls:
            return KnowledgeSearchResult(RetrievalStatus.LIMITED, (), self.calls_used)
        self.calls_used += 1
        try:
            raw = self._client.search(self._knowledge_base_ids, query, self._max_results)
            chunks = normalize_search_response(
                raw,
                tenant_id=self._tenant_id,
                agent_version=self._agent_version,
                session_id=self._session_id,
                knowledge_base_ids=self._knowledge_base_ids,
                media_signer=self._media_signer,
                max_results=self._max_results,
            )
        except KnowledgeSearchUnavailable:
            return KnowledgeSearchResult(RetrievalStatus.UNAVAILABLE, (), self.calls_used)
        return KnowledgeSearchResult(
            RetrievalStatus.FOUND if chunks else RetrievalStatus.NOT_FOUND,
            tuple(chunks),
            self.calls_used,
        )


class KbServiceClient:
    """Small stdlib HTTP client for the existing kb-service search contract."""

    def __init__(self, base_url: str, *, tenant_id: str, service_token: str = "", timeout: float = 10):
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("kb-service base URL must be an absolute URL without query or fragment")
        if not 0.5 <= timeout <= 60:
            raise ValueError("kb-service timeout must be between 0.5 and 60 seconds")
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.service_token = service_token.strip()
        self.timeout = timeout

    def for_tenant(self, tenant_id: str) -> KbServiceClient:
        """A copy bound to another tenant header (same base/token/timeout).

        The runtime builds one client per gateway process but the customer QA
        path serves multiple tenants; each job rebinds before searching.
        """
        return KbServiceClient(
            self.base_url,
            tenant_id=tenant_id,
            service_token=self.service_token,
            timeout=self.timeout,
        )

    def search(self, knowledge_base_ids: tuple[str, ...], question: str, top_k: int) -> list[dict[str, Any]]:
        if not knowledge_base_ids:
            return []
        merged: list[dict[str, Any]] = []
        for kb_id in knowledge_base_ids:
            path_id = urllib.parse.quote(kb_id, safe="")
            url = f"{self.base_url}/kb/knowledge-bases/{path_id}/search"
            headers = {"Content-Type": "application/json", "tenant-id": self.tenant_id}
            if self.service_token:
                headers["Authorization"] = f"Bearer {self.service_token}"
            request = urllib.request.Request(
                url,
                data=json.dumps({"question": question, "top_k": top_k}, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
                raise KnowledgeSearchUnavailable("kb-service search unavailable") from exc
            if not isinstance(payload, (dict, list)):
                raise KnowledgeSearchUnavailable("kb-service returned an invalid response")
            if isinstance(payload, dict):
                payload = payload.get("data", payload)
            chunks = payload.get("chunks", []) if isinstance(payload, dict) else payload
            if not isinstance(chunks, list):
                raise KnowledgeSearchUnavailable("kb-service returned invalid chunks")
            for chunk in chunks:
                if isinstance(chunk, dict):
                    merged.append({**chunk, "knowledge_base_id": kb_id})
        return merged

    def fetch_media(self, grant: MediaGrant, *, range_header: str | None = None) -> bytes:
        """Fetch the media bytes a signed grant refers to (#168 media plane).

        Image grants carry the RAGFlow ``image_id`` as ``backend_id`` and go
        through the kb-service image passthrough
        (``GET /kb/documents/images/{image_id}``); video grants carry the
        document id and go through the existing document download endpoint.
        Both are tenant-scoped by the ``tenant-id`` header. A missing blob is
        ``MediaNotFound`` (404); a transport failure is
        ``KnowledgeSearchUnavailable`` so the API layer can map it to 503.

        ``range_header`` (when the caller serves a Range request) is passed
        through to kb-service, so a video seek fetches only the requested
        bytes instead of buffering the whole object per request.
        """
        if grant.kind == "image":
            path = f"/kb/documents/images/{urllib.parse.quote(grant.backend_id, safe='')}"
        else:
            path = (
                f"/kb/knowledge-bases/{urllib.parse.quote(grant.knowledge_base_id, safe='')}"
                f"/documents/{urllib.parse.quote(grant.document_id, safe='')}/download"
            )
        url = f"{self.base_url}{path}"
        headers = {"tenant-id": self.tenant_id}
        if range_header:
            # Single-range only; parse_byte_range rejects the multi-range form
            # before we reach here, so replaying the header upstream is safe.
            headers["Range"] = range_header
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        request = urllib.request.Request(url, headers=headers, method="GET")

        def fetch_once() -> bytes:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if response.status == 404:
                        raise MediaNotFound(grant.backend_id)
                    body = response.read()
                    _raise_for_media_error_body(body)
                    return body
            except MediaNotFound:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise MediaNotFound(grant.backend_id) from exc
                raise KnowledgeSearchUnavailable("kb-service media fetch failed") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise KnowledgeSearchUnavailable("kb-service media fetch unavailable") from exc

        # RAGFlow can briefly report an existing video document as code=102
        # while its document index settles. Retry only that video-not-found
        # signal with bounded exponential backoff; real missing media still
        # ends as 404 after the budget is exhausted.
        attempts = 6 if grant.kind == "video" else 1
        for attempt in range(attempts):
            try:
                return fetch_once()
            except MediaNotFound:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.25 * (2**attempt))
        raise AssertionError("media fetch retry loop did not return")


def normalize_search_response(
    raw: Any,
    *,
    tenant_id: str,
    agent_version: str,
    session_id: str | None,
    knowledge_base_ids: tuple[str, ...],
    media_signer: MediaResourceSigner,
    max_results: int | None = None,
) -> list[RetrievedChunk]:
    if isinstance(raw, Mapping):
        raw = raw.get("data", raw)
        raw = raw.get("chunks", []) if isinstance(raw, Mapping) else raw
    if not isinstance(raw, list):
        return []
    allowed_kbs = set(knowledge_base_ids)
    result: list[RetrievedChunk] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kb_id = str(item.get("knowledge_base_id") or item.get("dataset_id") or "")
        if not kb_id:
            kb_id = knowledge_base_ids[0] if len(knowledge_base_ids) == 1 else ""
        if kb_id not in allowed_kbs:
            continue
        content = item.get("content_with_weight") or item.get("content") or item.get("text")
        if not isinstance(content, str) or not content.strip():
            continue
        chunk_id = _first_string(item, "chunk_id", "id")
        document_id = _first_string(item, "doc_id", "document_id")
        reference_id = chunk_id or document_id
        if not reference_id or not document_id:
            continue
        title = _first_string(item, "docnm_kwd", "doc_name", "document_name", "name")
        score = _score(item.get("score", item.get("similarity")))
        media: list[RetrievedMedia] = []
        image_id = _first_string(item, "image_id", "img_id")
        image_mime = _image_mime(item, title) if image_id else None
        if image_id and image_mime:
            resource = media_signer.issue(
                tenant_id=tenant_id,
                agent_version=agent_version,
                session_id=session_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                chunk_id=chunk_id,
                backend_id=image_id,
                kind="image",
                mime_type=image_mime,
                title=title,
                reference_id=reference_id,
            )
            media.append(RetrievedMedia(resource))
        doc_type = str(item.get("doc_type_kwd") or item.get("doc_type") or "").lower()
        if doc_type == "video" or _is_video_name(title):
            video_mime = _video_mime(item, title)
            if video_mime:
                resource = media_signer.issue(
                    tenant_id=tenant_id,
                    agent_version=agent_version,
                    session_id=session_id,
                    knowledge_base_id=kb_id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    backend_id=document_id,
                    kind="video",
                    mime_type=video_mime,
                    title=title,
                    reference_id=reference_id,
                )
                media.append(RetrievedMedia(resource))
        result.append(
            RetrievedChunk(
                reference_id=reference_id,
                content=redact_text(content),
                title=title,
                score=score,
                media=tuple(media),
            )
        )
    result.sort(key=lambda chunk: chunk.score if chunk.score is not None else float("-inf"), reverse=True)
    return result if max_results is None else result[:max_results]


def parse_byte_range(value: str, total: int) -> tuple[int, int]:
    if total < 0 or not value.startswith("bytes=") or "," in value:
        raise MediaRangeError("only one bytes range is supported")
    spec = value.removeprefix("bytes=").strip()
    if "-" not in spec:
        raise MediaRangeError("invalid byte range")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise MediaRangeError("invalid suffix range")
            start, end = max(total - suffix, 0), total - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else total - 1
            if start < 0 or end < start:
                raise MediaRangeError("invalid byte range")
            end = min(end, total - 1)
        if total == 0 or start >= total:
            raise MediaRangeError("range is outside the object")
    except (TypeError, ValueError) as exc:
        raise MediaRangeError("invalid byte range") from exc
    return start, end


def _split_resource_id(value: str) -> tuple[str, str]:
    candidate = value.rsplit("/", 1)[-1]
    if "." not in candidate:
        raise MediaAccessDenied("media resource is not available")
    bare, signature = candidate.rsplit(".", 1)
    # Format gate BEFORE any caller can encode()/compare_digest() the parts:
    # a public URL path may carry non-ASCII bytes (GET /v1/media/media_中文.sig
    # → UnicodeEncodeError, a non-ASCII signature → TypeError), which would
    # surface as a 500 instead of the uniform 403. Ids are always
    # "media_" + urlsafe-ascii + "." + 24 hex chars.
    if (
        not bare.startswith("media_")
        or not signature
        or not bare.isascii()
        or not signature.isascii()
        or len(bare) > 128
        or len(signature) > 64
    ):
        raise MediaAccessDenied("media resource is not available")
    return bare, signature


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo else current.replace(tzinfo=UTC)


def _first_string(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _score(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_video_name(value: str | None) -> bool:
    if not value:
        return False
    return value.lower().rsplit("?", 1)[0].endswith((".mp4", ".webm"))


def _image_mime(item: Mapping[str, Any], title: str | None) -> str | None:
    value = str(item.get("mime_type") or item.get("content_type") or "").lower()
    if value:
        return value if value in ALLOWED_IMAGE_TYPES else None
    guessed = mimetypes.guess_type(title or "")[0]
    return guessed if guessed in ALLOWED_IMAGE_TYPES else None


def _video_mime(item: Mapping[str, Any], title: str | None) -> str | None:
    value = str(item.get("mime_type") or item.get("content_type") or "").lower()
    if value:
        return value if value in ALLOWED_VIDEO_TYPES else None
    guessed = mimetypes.guess_type(title or "")[0]
    return guessed if guessed in ALLOWED_VIDEO_TYPES else "video/mp4"
