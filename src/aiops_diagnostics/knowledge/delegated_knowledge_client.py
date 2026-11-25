"""目标客服消费固定中央读能力；配置后不回退到共享KB直连。"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.common.bounded_http import (
    TRANSPORT_FAILURES,
    ErrorMapping,
    RequestSpec,
    bearer_auth_header,
    json_body,
    open_response,
    request_json,
)
from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint
from aiops_diagnostics.common.private_files import validate_private_directory, validate_private_file
from aiops_diagnostics.knowledge import knowledge_delegation as delegation
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KnowledgeSearchResult,
    KnowledgeSearchUnavailable,
    MediaResource,
    RetrievalStatus,
    RetrievedChunk,
    RetrievedMedia,
)


def load(path, environment):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in path.parents):
        raise ValueError("invalid delegated knowledge configuration")
    validate_private_directory(path.parent)
    validate_private_file(path)
    if path.stat().st_size > 262144:
        raise ValueError("delegated knowledge configuration too large")
    from aiops_diagnostics.onboarding.registry import _unique_object

    body = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    if (
        not isinstance(body, dict)
        or set(body) != {"schema", "base_url", "capabilities"}
        or body["schema"] != "aiops-knowledge-delegation/1"
    ):
        raise ValueError("invalid delegated knowledge configuration")
    if not isinstance(body["base_url"], str):
        raise ValueError("invalid delegated knowledge origin")
    base = require_secure_endpoint(body["base_url"], what="delegated knowledge")
    if urlsplit(base).path or base != body["base_url"]:
        raise ValueError("delegated knowledge requires a fixed gateway origin")
    rows = body["capabilities"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise ValueError("invalid delegated knowledge capabilities")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"knowledge_id", "token"}:
            raise ValueError("invalid delegated knowledge capability")
        scope = delegation.parse_id(row["knowledge_id"])
        token = row["token"]
        if (
            scope[0] != environment
            or not isinstance(token, str)
            or not token.startswith(row["knowledge_id"] + ".")
            or not re.fullmatch(re.escape(row["knowledge_id"]) + r"\.[a-f0-9]{64}", token)
        ):
            raise ValueError("delegated knowledge scope mismatch")
        key = (scope[1], scope[3])
        if key in found:
            raise ValueError("duplicate delegated knowledge application")
        found[key] = (scope, token)
    return base, found


def for_application(runtime, tenant, app):
    settings = runtime.gateway_settings
    path = getattr(settings, "knowledge_delegation_file", "")
    if not path:
        return None
    try:
        base, values = load(path, settings.execution_environment)
        scope, token = values[(tenant, app)]
        return DelegatedKnowledgeClient(base, scope, token, settings.kb_service_timeout_seconds)
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        raise KnowledgeSearchUnavailable("delegated knowledge unavailable") from exc


class DelegatedKnowledgeClient:
    def __init__(self, base, scope, token, timeout):
        self.base, self.scope, self.token, self.timeout = base, scope, token, timeout

    def _request(self, path, body=None):
        def unavailable(_):
            return KnowledgeSearchUnavailable("delegated knowledge unavailable")

        return request_json(
            RequestSpec(
                url=self.base + path,
                method="GET" if body is None else "POST",
                headers={"Authorization": bearer_auth_header(self.token), "Content-Type": "application/json"},
                body=None if body is None else json_body(body),
                timeout=self.timeout,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=unavailable,
                http_error=unavailable,
                unavailable=unavailable,
                invalid_body=unavailable,
                invalid_envelope=unavailable,
            ),
        )

    def list_knowledge_bases(self):
        body = self._request("/v1/dify/knowledge")
        rows = body.get("knowledge_items") if isinstance(body, dict) else None
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("knowledge_id") != delegation.identifier(self.scope)
            or rows[0].get("knowledge_base_ids") != [self.scope[4]]
        ):
            raise KnowledgeSearchUnavailable("delegated knowledge scope changed")
        return [{"knowledge_base_id": self.scope[4], "name": rows[0].get("name", ""), "audience": "customer"}]

    def search(self, libraries, query, top_k, *, media_signer=None, agent_version="", session_id=None):
        if tuple(libraries) != (self.scope[4],):
            raise KnowledgeSearchUnavailable("delegated knowledge scope changed")
        body = self._request(
            "/v1/dify/retrieval",
            {
                "knowledge_id": delegation.identifier(self.scope),
                "query": query,
                "retrieval_setting": {"top_k": top_k},
            },
        )
        try:
            records = body["records"]
            if not isinstance(records, list) or len(records) > top_k:
                raise ValueError()
            chunks = []
            for row in records:
                reference = row["metadata"]["reference_id"]
                if not isinstance(reference, str) or not isinstance(row["content"], str):
                    raise ValueError()
                media = []
                for item in row["metadata"]["media"]:
                    url = item["url"]
                    parsed = urlsplit(url)
                    if item["kind"] not in {"image", "video"} or not isinstance(item["resource_id"], str):
                        raise ValueError()
                    if not (
                        url.startswith(self.base + "/v1/media/") and not parsed.query and not parsed.fragment
                    ):
                        from aiops_diagnostics.knowledge.knowledge_retrieval import _external_video_link

                        if item["kind"] != "video" or _external_video_link(url)[0] != url:
                            raise ValueError()
                    resource = MediaResource(
                        item["resource_id"], url, item["kind"], item["mime_type"], row.get("title"), reference
                    )
                    if media_signer is not None:
                        signed = (
                            url.removeprefix(self.base + "/v1/media/")
                            if url.startswith(self.base + "/v1/media/")
                            else ""
                        )
                        resource = media_signer.issue(
                            tenant_id=self.scope[1],
                            agent_version=agent_version,
                            session_id=session_id,
                            knowledge_base_id=self.scope[4],
                            document_id=reference,
                            chunk_id=reference,
                            backend_id=signed,
                            kind=item["kind"],
                            mime_type=item["mime_type"],
                            title=row.get("title"),
                            reference_id=reference,
                            external_url="" if signed else url,
                        )
                    media.append(RetrievedMedia(resource))
                chunks.append(
                    RetrievedChunk(
                        reference, row["content"], row.get("title"), row.get("score"), tuple(media)
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise KnowledgeSearchUnavailable("invalid delegated knowledge response") from exc
        return KnowledgeSearchResult(
            RetrievalStatus.FOUND if chunks else RetrievalStatus.NOT_FOUND, tuple(chunks), 1
        )

    def fetch_media(self, grant, *, range_header=None):
        from aiops_diagnostics.knowledge.knowledge_retrieval import MediaAccessDenied, _split_resource_id

        if grant.tenant_id != self.scope[1] or grant.knowledge_base_id != self.scope[4]:
            raise MediaAccessDenied("delegated media scope mismatch")
        _split_resource_id(grant.backend_id)
        if "/" in grant.backend_id:
            raise MediaAccessDenied("delegated media path invalid")
        self.list_knowledge_bases()
        headers = {"Authorization": bearer_auth_header(self.token)}
        if range_header:
            headers["Range"] = range_header
        from urllib.error import HTTPError

        from aiops_diagnostics.knowledge.knowledge_retrieval import MediaResponse, read_media_response

        try:
            with open_response(
                RequestSpec(
                    url=self.base + "/v1/media/" + grant.backend_id,
                    method="GET",
                    headers=headers,
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            ) as response:
                body = read_media_response(response, max_bytes=32 * 1024 * 1024)
        except HTTPError as exc:
            if exc.code != 416:
                raise KnowledgeSearchUnavailable("delegated media unavailable") from exc
            body = MediaResponse(416, {"Content-Range": exc.headers.get("Content-Range", "")}, b"")
        except TRANSPORT_FAILURES + (ValueError,) as exc:
            raise KnowledgeSearchUnavailable("delegated media unavailable") from exc
        self.list_knowledge_bases()
        return body
