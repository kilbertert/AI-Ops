from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.private_files import append_private_text
from aiops_diagnostics.redaction import sanitize_data


@dataclass(frozen=True, slots=True)
class JournalEntry:
    evidence_id: str
    incident_id: str
    tool: str
    source: str
    status: Literal["success", "failed", "blocked"]
    request: dict[str, Any]
    artifact: str
    artifact_sha256: str
    row_count: int | None
    error: str | None
    created_at: str


class EvidenceJournal:
    def __init__(self, workspace: AgentWorkspace, manifest: IncidentManifest) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.path = workspace.path / "evidence-journal.jsonl"

    def record(
        self,
        *,
        tool: ToolName,
        source: str,
        status: Literal["success", "failed", "blocked"],
        request: dict[str, Any],
        payload: Any,
        error: str | None = None,
        row_count: int | None = None,
    ) -> JournalEntry:
        evidence_id = f"ev-{len(self.entries()) + 1:03d}"
        sanitized = sanitize_data(
            payload,
            preserve=(self.manifest.order_no, self.manifest.tenant_id or ""),
        )
        artifact_relative = f"evidence/{evidence_id}.json"
        artifact_path = self.workspace.write_json(
            artifact_relative,
            {
                "evidence_id": evidence_id,
                "incident_id": self.manifest.incident_id,
                "tool": tool.value,
                "source": source,
                "status": status,
                "payload": sanitized,
                "error": sanitize_data(error, preserve=(self.manifest.order_no,)) if error else None,
            },
        )
        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        entry = JournalEntry(
            evidence_id=evidence_id,
            incident_id=self.manifest.incident_id,
            tool=tool.value,
            source=source,
            status=status,
            request=sanitize_data(request, preserve=(self.manifest.order_no,)),
            artifact=artifact_relative,
            artifact_sha256=artifact_hash,
            row_count=row_count,
            error=sanitize_data(error, preserve=(self.manifest.order_no,)) if error else None,
            created_at=datetime.now(UTC).isoformat(),
        )
        append_private_text(
            self.path,
            json.dumps(asdict(entry), ensure_ascii=False, default=str) + "\n",
        )
        return entry

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        result: list[JournalEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(JournalEntry(**json.loads(line)))
        return result

    def get(self, evidence_id: str) -> JournalEntry | None:
        return next((entry for entry in self.entries() if entry.evidence_id == evidence_id), None)

    def latest_success(self, tool: ToolName) -> JournalEntry | None:
        latest = next((entry for entry in reversed(self.entries()) if entry.tool == tool.value), None)
        return latest if latest and latest.status == "success" else None

    def load_payload(self, entry: JournalEntry) -> Any:
        path = self.artifact_path(entry)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.artifact_sha256:
            raise ValueError(f"证据文件缺失或哈希不匹配: {entry.evidence_id}")
        artifact = self.workspace.read_json(entry.artifact)
        return artifact.get("payload")

    def failed_sources(self) -> list[str]:
        sources = [entry.source for entry in self.entries() if entry.status == "failed"]
        for entry in self.entries():
            if entry.tool != ToolName.KNOWN_RUNBOOK.value or entry.status != "success":
                continue
            try:
                payload = self.load_payload(entry)
            except ValueError:
                sources.append("harness:evidence_integrity")
                continue
            report = payload.get("report", {}) if isinstance(payload, dict) else {}
            sources.extend(str(item) for item in report.get("failed_sources", []))
        return list(dict.fromkeys(sources))

    def artifact_path(self, entry: JournalEntry) -> Path:
        return self.workspace._resolve(entry.artifact)
