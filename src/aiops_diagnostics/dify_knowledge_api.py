"""The Dify-facing knowledge retrieval adapter (#581 / PRD #577).

Dify's External Knowledge API is the contract here, verified from Dify's own
source (`api/services/external_knowledge_service.py`):

* the endpoint is called as ``POST {endpoint}/retrieval`` with a Bearer key;
* the body is exactly ``retrieval_setting{top_k, score_threshold}``, ``query``,
  ``knowledge_id``, ``metadata_condition``;
* a 200 body is read as ``response.json().get("records", [])`` — any other
  status becomes ``ValueError(response.text)`` on their side;
* ``dataset_retrieval.py`` only reads a record's ``score`` and ``title`` when
  its ``metadata`` is not None.

This module owns the *shape* of that exchange and the record rendering. The
route in ``gateway_api`` owns who may call it and where the tenant comes from.
Keeping them apart is the point: Dify has **no tenant concept**, so the tenant
and the knowledge-base allow-list can only come from our own registry — which
is what stops this from being a "Dify asks, we give" back door.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.knowledge_retrieval import RetrievedChunk

#: Dify appends ``/retrieval`` itself, so the operator's "API Endpoint" is our
#: base path and this is the route it reaches.
DIFY_RETRIEVAL_PATH = "/v1/dify/retrieval"

DIFY_INVALID_REQUEST = "DIFY_INVALID_REQUEST"
#: A capability we deliberately do not implement. 400 rather than a silent
#: ignore: quietly dropping the operator's metadata filter would answer with
#: records the filter excluded while looking like it had been applied.
DIFY_FILTER_UNSUPPORTED = "DIFY_FILTER_UNSUPPORTED"


class DifyRequestError(ValueError):
    """A request that cannot be served as written, with its own error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DifyRetrievalRequest:
    query: str
    knowledge_id: str
    top_k: int | None
    score_threshold: float


def parse_dify_retrieval_request(
    payload: Any,
    *,
    max_top_k: int,
) -> DifyRetrievalRequest:
    """Read Dify's request body, capping ``top_k`` at our own limit.

    ``top_k`` is *capped*, never taken at face value: Dify's value is an
    operator's UI preference, while the allow-list and the result budget are
    the harness's. Same rule as ``KnowledgeSearchGuard``'s ``max_results``.
    """
    if not isinstance(payload, Mapping):
        raise DifyRequestError(DIFY_INVALID_REQUEST, "request body must be a JSON object")

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise DifyRequestError(DIFY_INVALID_REQUEST, "query must be a non-empty string")

    knowledge_id = payload.get("knowledge_id")
    if not isinstance(knowledge_id, str) or not knowledge_id.strip():
        raise DifyRequestError(DIFY_INVALID_REQUEST, "knowledge_id must be a non-empty string")

    metadata_condition = payload.get("metadata_condition")
    if metadata_condition:
        raise DifyRequestError(
            DIFY_FILTER_UNSUPPORTED,
            "metadata_condition is not supported by this knowledge source",
        )

    setting = payload.get("retrieval_setting")
    raw_top_k: Any = None
    raw_threshold: Any = None
    if isinstance(setting, Mapping):
        raw_top_k = setting.get("top_k")
        raw_threshold = setting.get("score_threshold")
    elif setting is not None:
        raise DifyRequestError(DIFY_INVALID_REQUEST, "retrieval_setting must be an object")

    top_k: int | None = None
    if raw_top_k is not None:
        if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool) or raw_top_k < 1:
            raise DifyRequestError(DIFY_INVALID_REQUEST, "retrieval_setting.top_k must be a positive integer")
        top_k = min(raw_top_k, max_top_k)

    threshold = 0.0
    if raw_threshold is not None:
        if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
            raise DifyRequestError(DIFY_INVALID_REQUEST, "retrieval_setting.score_threshold must be a number")
        # Clamped into [0, 1] instead of rejected: Dify's own UI writes
        # 0.0/`score_threshold_enabled=false`, and a threshold outside the
        # range is a "no opinion" rather than a malformed request.
        threshold = min(max(float(raw_threshold), 0.0), 1.0)

    return DifyRetrievalRequest(
        query=query.strip(),
        knowledge_id=knowledge_id.strip(),
        top_k=top_k,
        score_threshold=threshold,
    )


def dify_records(
    chunks: Sequence[RetrievedChunk],
    *,
    score_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """Render normalized chunks as Dify ``records``.

    ``metadata`` is **always** a non-null object. Dify reads ``score`` and
    ``title`` off a record only when ``metadata is not None``; a null metadata
    would silently drop both on their side while our response looked complete.

    Media never travels as a bare signed path: the record carries the URL our
    own runtime already mints, so Dify's chat UI can render it, and no RAGFlow
    identifier or kb-service port is exposed.
    """
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        if score_threshold > 0.0 and (chunk.score is None or chunk.score < score_threshold):
            continue
        records.append(
            {
                "content": chunk.content,
                "score": chunk.score,
                "title": chunk.title,
                "metadata": {
                    "reference_id": chunk.reference_id,
                    "media": [
                        {
                            "url": item.resource.url,
                            "kind": item.resource.kind,
                            "mime_type": item.resource.mime_type,
                        }
                        for item in chunk.media
                    ],
                },
            }
        )
    return records
