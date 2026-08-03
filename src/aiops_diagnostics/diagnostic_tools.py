from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName, ToolRequest
from aiops_diagnostics.engine import DiagnosticEngine, _order_window
from aiops_diagnostics.journal import EvidenceJournal, JournalEntry
from aiops_diagnostics.models import DiagnosticRequest
from aiops_diagnostics.sources import DiagnosticSources, SourceError

TOOL_DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.ORDER_SNAPSHOT: "MySQL order rows scoped by immutable order_no and optional tenant_id.",
    ToolName.FEE_SNAPSHOT: "MySQL fee-template snapshot for amount and tariff verification.",
    ToolName.DEVICE_SNAPSHOT: "MySQL device protocol, online, work-status, and error metadata.",
    ToolName.GUN_TIMESERIES: "Bounded TDengine gun status and telemetry for the order time window.",
    ToolName.COMM_MESSAGES: "Bounded TDengine decoded communication messages for the order window.",
    ToolName.REDIS_SYNC: "Bounded Redis Stream metadata and order-match counts for synchronization cases.",
    ToolName.KNOWN_RUNBOOK: "Existing deterministic diagnostic report as advisory evidence.",
}

TOOL_SOURCES: dict[ToolName, str] = {
    ToolName.ORDER_SNAPSHOT: "mysql:ch_order_info",
    ToolName.FEE_SNAPSHOT: "mysql:ch_fee_template_record",
    ToolName.DEVICE_SNAPSHOT: "mysql:iot_charging_device",
    ToolName.GUN_TIMESERIES: "tdengine:charging-gun_property",
    ToolName.COMM_MESSAGES: "tdengine:charging-pile_comm",
    ToolName.REDIS_SYNC: "redis:order_sync_streams",
    ToolName.KNOWN_RUNBOOK: "deterministic:known_runbook",
}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    tool: ToolName
    status: str
    evidence_id: str
    artifact: str
    source: str
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool.value,
            "status": self.status,
            "evidence_id": self.evidence_id,
            "artifact": self.artifact,
            "source": self.source,
            "reused": self.reused,
        }


class DiagnosticToolExecutor:
    def __init__(
        self,
        sources: DiagnosticSources,
        request: DiagnosticRequest,
        manifest: IncidentManifest,
        journal: EvidenceJournal,
        *,
        safety: Any,
    ) -> None:
        self.sources = sources
        self.request = request
        self.manifest = manifest
        self.journal = journal
        self.safety = safety

    def execute_many(self, requests: list[ToolRequest]) -> list[ToolOutcome]:
        return [self.execute(item.tool) for item in requests]

    def execute(self, tool: ToolName) -> ToolOutcome:
        cached = self.journal.latest_success(tool)
        if cached:
            try:
                self.journal.load_payload(cached)
            except ValueError as exc:
                entry = self.journal.record(
                    tool=tool,
                    source="harness:evidence_integrity",
                    status="failed",
                    request=self._identity_request(),
                    payload={"evidence_id": cached.evidence_id},
                    error=str(exc),
                )
                return self._outcome(tool, entry)
            return self._outcome(tool, cached, reused=True)
        try:
            entry = {
                ToolName.ORDER_SNAPSHOT: self._order_snapshot,
                ToolName.FEE_SNAPSHOT: self._fee_snapshot,
                ToolName.DEVICE_SNAPSHOT: self._device_snapshot,
                ToolName.GUN_TIMESERIES: self._gun_timeseries,
                ToolName.COMM_MESSAGES: self._comm_messages,
                ToolName.REDIS_SYNC: self._redis_sync,
                ToolName.KNOWN_RUNBOOK: self._known_runbook,
            }[tool]()
        except (SourceError, ValueError) as exc:
            entry = self.journal.record(
                tool=tool,
                source=TOOL_SOURCES[tool],
                status="failed",
                request=self._identity_request(),
                payload={"exception_type": exc.__class__.__name__},
                error=str(exc),
            )
        return self._outcome(tool, entry)

    def _order_snapshot(self) -> JournalEntry:
        rows = self.sources.get_orders(self.request.order_no, self.request.tenant_id)
        return self.journal.record(
            tool=ToolName.ORDER_SNAPSHOT,
            source=TOOL_SOURCES[ToolName.ORDER_SNAPSHOT],
            status="success",
            request=self._identity_request(),
            payload={"orders": rows},
            row_count=len(rows),
        )

    def _fee_snapshot(self) -> JournalEntry:
        _, blocked = self._single_order(ToolName.FEE_SNAPSHOT)
        if blocked:
            return blocked
        record = self.sources.get_fee_template_record(
            self.request.order_no,
            self.request.tenant_id,
        )
        return self.journal.record(
            tool=ToolName.FEE_SNAPSHOT,
            source=TOOL_SOURCES[ToolName.FEE_SNAPSHOT],
            status="success",
            request=self._identity_request(),
            payload={"fee_template_record": record},
            row_count=1 if record else 0,
        )

    def _device_snapshot(self) -> JournalEntry:
        order, blocked = self._single_order(ToolName.DEVICE_SNAPSHOT)
        if blocked:
            return blocked
        device = self.sources.get_device(
            order.get("device_id"),
            order.get("device_code"),
            order.get("tenant_id"),
        )
        return self.journal.record(
            tool=ToolName.DEVICE_SNAPSHOT,
            source=TOOL_SOURCES[ToolName.DEVICE_SNAPSHOT],
            status="success",
            request={"device_id": order.get("device_id"), "device_code": order.get("device_code")},
            payload={"device": device},
            row_count=1 if device else 0,
        )

    def _gun_timeseries(self) -> JournalEntry:
        order, blocked = self._single_order(ToolName.GUN_TIMESERIES)
        if blocked:
            return blocked
        device = order.get("child_device_code") or order.get("device_code")
        window = _order_window(order, self.safety.max_order_window_hours)
        if not device or not window:
            return self._blocked(
                ToolName.GUN_TIMESERIES,
                "订单缺少设备编码或有效时间范围",
            )
        start_time, end_time, clamped = window
        tx_serial_no = _transaction_serial(order)
        rows = self.sources.get_gun_samples(device, start_time, end_time, tx_serial_no)
        return self.journal.record(
            tool=ToolName.GUN_TIMESERIES,
            source=TOOL_SOURCES[ToolName.GUN_TIMESERIES],
            status="success",
            request={
                "device": device,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "tx_serial_no": tx_serial_no,
                "window_clamped": clamped,
            },
            payload={"samples": rows, "window_clamped": clamped},
            row_count=len(rows),
        )

    def _comm_messages(self) -> JournalEntry:
        order, blocked = self._single_order(ToolName.COMM_MESSAGES)
        if blocked:
            return blocked
        device = order.get("device_code") or order.get("child_device_code")
        window = _order_window(order, self.safety.max_order_window_hours)
        if not device or not window:
            return self._blocked(
                ToolName.COMM_MESSAGES,
                "订单缺少设备编码或有效时间范围",
            )
        start_time, end_time, clamped = window
        rows = self.sources.get_comm_messages(device, start_time, end_time)
        return self.journal.record(
            tool=ToolName.COMM_MESSAGES,
            source=TOOL_SOURCES[ToolName.COMM_MESSAGES],
            status="success",
            request={
                "device": device,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "window_clamped": clamped,
            },
            payload={"messages": rows, "window_clamped": clamped},
            row_count=len(rows),
        )

    def _redis_sync(self) -> JournalEntry:
        _, blocked = self._single_order(ToolName.REDIS_SYNC)
        if blocked:
            return blocked
        rows = self.sources.inspect_streams(self.request.order_no)
        return self.journal.record(
            tool=ToolName.REDIS_SYNC,
            source=TOOL_SOURCES[ToolName.REDIS_SYNC],
            status="success",
            request={"order_no": self.request.order_no},
            payload={"streams": rows},
            row_count=len(rows),
        )

    def _known_runbook(self) -> JournalEntry:
        _, blocked = self._single_order(ToolName.KNOWN_RUNBOOK)
        if blocked:
            return blocked
        report = DiagnosticEngine(self.sources, self.safety).diagnose(self.request)
        return self.journal.record(
            tool=ToolName.KNOWN_RUNBOOK,
            source=TOOL_SOURCES[ToolName.KNOWN_RUNBOOK],
            status="success",
            request=self._identity_request(),
            payload={"report": report.to_dict()},
            row_count=len(report.evidence),
        )

    def _single_order(self, tool: ToolName) -> tuple[dict[str, Any], JournalEntry | None]:
        entry = self.journal.latest_success(ToolName.ORDER_SNAPSHOT)
        if not entry:
            return {}, self._blocked(tool, "必须先请求 order_snapshot")
        payload = self.journal.load_payload(entry)
        orders = payload.get("orders", []) if isinstance(payload, dict) else []
        if len(orders) != 1:
            return {}, self._blocked(tool, f"order_snapshot 返回 {len(orders)} 行，无法唯一选定订单")
        return orders[0], None

    def _blocked(self, tool: ToolName, reason: str) -> JournalEntry:
        return self.journal.record(
            tool=tool,
            source="harness:dependency",
            status="blocked",
            request=self._identity_request(),
            payload={"reason": reason},
            error=reason,
        )

    def _identity_request(self) -> dict[str, Any]:
        return {
            "order_no": self.request.order_no,
            "tenant_id": self.request.tenant_id,
        }

    @staticmethod
    def _outcome(tool: ToolName, entry: JournalEntry, *, reused: bool = False) -> ToolOutcome:
        return ToolOutcome(
            tool=tool,
            status=entry.status,
            evidence_id=entry.evidence_id,
            artifact=entry.artifact,
            source=entry.source,
            reused=reused,
        )


def _transaction_serial(order: dict[str, Any]) -> str | None:
    protocol = str(order.get("device_protocol") or "").upper()
    if protocol.startswith("OCPP"):
        value = order.get("transaction_id")
        if value not in (None, ""):
            return str(value)
        tx_data = order.get("tx_data")
        if isinstance(tx_data, dict) and tx_data.get("txSerialNo") not in (None, ""):
            return str(tx_data["txSerialNo"])
        return None
    value = order.get("order_no")
    return str(value) if value not in (None, "") else None
