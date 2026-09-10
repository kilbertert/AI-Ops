from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aiops_diagnostics.models import DiagnosticRequest
from aiops_diagnostics.redaction import redact_text


class DiagnosisStatus(StrEnum):
    DIAGNOSED = "diagnosed"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ToolName(StrEnum):
    ORDER_SNAPSHOT = "order_snapshot"
    FEE_SNAPSHOT = "fee_snapshot"
    DEVICE_SNAPSHOT = "device_snapshot"
    GUN_TIMESERIES = "gun_timeseries"
    COMM_MESSAGES = "comm_messages"
    REDIS_SYNC = "redis_sync"
    KNOWN_RUNBOOK = "known_runbook"


class IncidentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    incident_id: str
    order_no: str
    tenant_id: str | None
    problem: str
    intent: str
    source_hash: str

    @model_validator(mode="after")
    def validate_identity_hash(self) -> IncidentManifest:
        source_hash = _incident_source_hash(
            self.order_no,
            self.tenant_id,
            self.problem,
            self.intent,
        )
        if self.source_hash != source_hash:
            raise ValueError("incident source_hash 与内容不一致")
        if self.incident_id != f"incident-{source_hash[:16]}":
            raise ValueError("incident_id 与 source_hash 不一致")
        return self

    @classmethod
    def from_request(cls, request: DiagnosticRequest) -> IncidentManifest:
        problem = redact_text(request.problem, preserve=(request.order_no,))
        source_hash = _incident_source_hash(
            request.order_no,
            request.tenant_id,
            problem,
            request.intent.value,
        )
        return cls(
            incident_id=f"incident-{source_hash[:16]}",
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            problem=problem,
            intent=request.intent.value,
            source_hash=source_hash,
        )


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    reason: str = Field(min_length=1, max_length=500)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class AgentDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    incident_id: str
    order_no: str
    tenant_id: str | None
    status: DiagnosisStatus
    summary: str = Field(min_length=1, max_length=2000)
    root_cause: str = Field(min_length=1, max_length=4000)
    confidence: Confidence
    evidence_ids: list[str] = Field(max_length=50)
    hypotheses: list[Hypothesis] = Field(max_length=10)
    limitations: list[str] = Field(max_length=30)
    failed_sources: list[str] = Field(max_length=20)
    next_steps: list[str] = Field(max_length=20)


class AgentTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool_requests", "diagnosis"]
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=7)
    diagnosis: AgentDiagnosis | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> AgentTurn:
        if self.kind == "tool_requests":
            if not self.tool_requests or self.diagnosis is not None:
                raise ValueError("tool_requests turn requires requests and no diagnosis")
        elif self.diagnosis is None or self.tool_requests:
            raise ValueError("diagnosis turn requires diagnosis and no tool requests")
        return self


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    incident_id: str
    thread_id: str | None = None
    phase: Literal["created", "running", "interrupted", "completed", "blocked"] = "created"
    turn_count: int = 0
    tool_call_count: int = 0
    validation_attempts: int = 0
    next_prompt: str
    fixture_path: str | None = None
    fixture_sha256: str | None = None
    provider_base_url: str
    provider: str = ""
    key_slot: str


def agent_turn_schema() -> dict[str, Any]:
    schema = AgentTurn.model_json_schema()
    _make_strict_response_schema(schema)
    return schema


QA_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "回答正文，简体中文"},
        "reminder": {
            "type": "boolean",
            "description": "是否在回答末尾提示用户提供订单号可获得更精确结果",
        },
    },
    "required": ["text", "reminder"],
    "additionalProperties": False,
}


def qa_turn_schema() -> dict[str, Any]:
    """Strict structured-output schema for the zero-order general assistant."""
    return QA_TURN_SCHEMA


class QaBlock(BaseModel):
    """One `blocks[]` content block in the customer QA output contract (blocks-v1).

    `kind` selects the shape:
      - text: `text` carries the prose (Simplified Chinese).
      - image / video: `resource_id` must name a media resource issued to THIS
        turn by the harness's knowledge_search tool; the model may not invent
        URLs. `title` is optional display metadata.
      - reference: `reference_id` must name a chunk returned by THIS turn's
        knowledge_search; `title` is the source document name.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text", "image", "video", "reference"]
    text: str = ""
    resource_id: str = ""
    reference_id: str = ""
    title: str = ""

    @model_validator(mode="after")
    def validate_payload(self) -> QaBlock:
        if self.kind == "text":
            if not self.text.strip():
                raise ValueError("text block requires non-empty text")
            if self.resource_id or self.reference_id:
                raise ValueError("text block must not carry resource/reference ids")
        elif self.kind in ("image", "video"):
            if not self.resource_id.startswith("media_"):
                raise ValueError(f"{self.kind} block requires a harness-issued media resource id")
            if self.text:
                raise ValueError(f"{self.kind} block must not carry text")
        else:
            if not self.reference_id:
                raise ValueError("reference block requires a reference id")
            if self.text or self.resource_id:
                raise ValueError("reference block must not carry text or resource ids")
        return self

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.kind == "text":
            payload["text"] = self.text
        elif self.kind in ("image", "video"):
            payload["resource_id"] = self.resource_id
            if self.title:
                payload["title"] = self.title
        else:
            payload["reference_id"] = self.reference_id
            if self.title:
                payload["title"] = self.title
        return payload


class QaAnswer(BaseModel):
    """The blocks-v1 customer QA answer the harness stores for the qa job."""

    model_config = ConfigDict(extra="forbid")

    blocks: list[QaBlock] = Field(min_length=1, max_length=40)
    retrieval_status: Literal["found", "not_found", "unavailable", "limited"]

    @model_validator(mode="after")
    def validate_blocks(self) -> QaAnswer:
        if not any(block.kind == "text" for block in self.blocks):
            raise ValueError("blocks must contain at least one text block")
        return self

    def to_public_dict(self, *, media_by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Public `result` payload for the assistant QA poll response.

        Media blocks are enriched with the signed media resource descriptor from
        this turn's retrieval so the frontend can render without parsing URLs.
        `media_by_id` maps resource_id -> MediaResource.to_dict().
        """
        media_by_id = media_by_id or {}
        blocks: list[dict[str, Any]] = []
        for block in self.blocks:
            payload = block.to_dict()
            if block.kind in ("image", "video"):
                resource = media_by_id.get(block.resource_id)
                if resource is not None:
                    payload["media"] = resource
                else:
                    # The referenced grant expired or was invalidated mid-run;
                    # keep the block but flag it unavailable so text survives.
                    payload["media"] = None
                    payload["unavailable"] = True
            blocks.append(payload)
        return {
            "blocks": blocks,
            "retrieval_status": self.retrieval_status,
        }


QA_RAG_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["tool_requests", "answer"],
        },
        "tool_requests": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": ["knowledge_search"]},
                    "reason": {"type": "string", "maxLength": 500},
                    "query": {"type": "string", "maxLength": 400},
                },
                "required": ["tool", "reason", "query"],
                "additionalProperties": False,
            },
        },
        "answer": {
            "type": "object",
            "properties": {
                "blocks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 40,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["text", "image", "video", "reference"]},
                            "text": {"type": "string"},
                            "resource_id": {"type": "string"},
                            "reference_id": {"type": "string"},
                            "title": {"type": "string"},
                        },
                        "required": ["kind", "text", "resource_id", "reference_id", "title"],
                        "additionalProperties": False,
                    },
                },
                "retrieval_status": {
                    "type": "string",
                    "enum": ["found", "not_found", "unavailable", "limited"],
                },
            },
            "required": ["blocks", "retrieval_status"],
            "additionalProperties": False,
        },
    },
    "required": ["kind", "tool_requests", "answer"],
    "additionalProperties": False,
}


def qa_rag_turn_schema() -> dict[str, Any]:
    """Strict structured-output schema for the customer QA RAG harness turn."""
    return QA_RAG_TURN_SCHEMA


def _make_strict_response_schema(node: Any) -> None:
    """Normalize Pydantic output for strict Responses API providers."""
    if isinstance(node, list):
        for item in node:
            _make_strict_response_schema(item)
        return
    if not isinstance(node, dict):
        return

    node.pop("default", None)
    properties = node.get("properties")
    if isinstance(properties, dict):
        node["additionalProperties"] = False
        node["required"] = list(properties)
    for value in node.values():
        _make_strict_response_schema(value)


def _incident_source_hash(order_no: str, tenant_id: str | None, problem: str, intent: str) -> str:
    source = {
        "order_no": order_no,
        "tenant_id": tenant_id,
        "problem": problem,
        "intent": intent,
    }
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
