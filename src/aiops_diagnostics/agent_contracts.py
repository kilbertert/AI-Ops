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
