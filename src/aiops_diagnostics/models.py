from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    GENERAL = "general"
    AMOUNT = "amount"
    ABNORMAL_STOP = "abnormal_stop"
    START_FAILURE = "start_failure"
    OFFLINE = "offline"
    SYNC = "sync"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(slots=True)
class DiagnosticRequest:
    order_no: str
    problem: str
    intent: Intent
    tenant_id: str | None = None


@dataclass(slots=True)
class Evidence:
    source: str
    title: str
    observation: str
    severity: Severity = Severity.INFO
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiagnosticReport:
    request: DiagnosticRequest
    summary: str
    classifications: list[str] = field(default_factory=list)
    confidence: str = "low"
    order_facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    queried_sources: list[str] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_eng_string"):
        return value.to_eng_string()
    return value
