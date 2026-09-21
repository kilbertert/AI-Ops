from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName, ToolRequest
from aiops_diagnostics.engine import DiagnosticEngine, _order_window
from aiops_diagnostics.journal import EvidenceJournal, JournalEntry
from aiops_diagnostics.models import DiagnosticRequest
from aiops_diagnostics.order_visibility import (
    TENANT_SCOPE_SOURCE,
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    visible_orders,
)
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import DiagnosticSources, SourceError, TDengineSource

_LOGGER = logging.getLogger("aiops.diagnostic_tools")

TOOL_DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.ORDER_SNAPSHOT: "MySQL order rows by order_no; tenant learned from the order, scope-checked.",
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


def preflight_environment(
    sources: DiagnosticSources,
    request: DiagnosticRequest,
    journal: EvidenceJournal,
) -> tuple[str, ...]:
    """Run start environment preflight: one doctor() consultation.

    Records a ``blocked`` journal entry for every evidence channel the doctor
    already knows is absent (missing stable / missing column) and returns
    advisory notes for the initial prompt, so the model plans around the gaps
    from turn 1 instead of spending a turn hitting a guaranteed SourceError.

    Advisory only: ``DiagnosticToolExecutor.execute`` never refuses — a tool
    the model still requests runs for real and fails naturally (the journal
    then carries the honest ``failed`` entry). A doctor that raises or is
    absent (FixtureSources) disables the preflight entirely; tools then fail
    naturally as before.
    """
    doctor = getattr(sources, "doctor", None)
    if doctor is None:
        return ()
    try:
        report = doctor()
    except Exception:
        # 预检保持 advisory（不让 doctor 故障挡诊断），但静默失效必须留痕：
        # server-side 一行日志，避免"预检为何没生效"成为盲区
        _LOGGER.warning("环境预检 doctor 调用失败，本次运行跳过预检: %s", request.order_no, exc_info=True)
        return ()
    tdengine = report.get("tdengine") if isinstance(report, dict) else None
    details = tdengine.get("details") if isinstance(tdengine, dict) else None
    if not isinstance(details, dict):
        return ()
    gun_columns = details.get("gun_columns") or {}

    gaps: list[tuple[ToolName, str]] = []
    if not details.get("charging_gun_property"):
        gaps.append((ToolName.GUN_TIMESERIES, "charging-gun_property 表不存在"))
    else:
        missing_columns = [column for column in TDengineSource.GUN_COLUMNS if not gun_columns.get(column)]
        if missing_columns:
            gaps.append(
                (ToolName.GUN_TIMESERIES, f"charging-gun_property 缺少列 {'、'.join(missing_columns)}")
            )
    if not details.get("charging_pile_comm"):
        gaps.append((ToolName.COMM_MESSAGES, "charging-pile_comm 表不存在"))

    notes: list[str] = []
    for tool, reason in gaps:
        error = f"环境数据面缺口: {reason}（预检）"
        journal.record(
            tool=tool,
            source=TOOL_SOURCES[tool],
            status="blocked",
            request={"order_no": request.order_no, "tenant_id": request.tenant_id},
            payload={"reason": reason, "preflight": True},
            error=error,
        )
        notes.append(error)
    return tuple(notes)


def _tenant_visibility(scope: QueryScope | None, allowed_tenants: set[str] | None) -> TenantVisibility:
    """Render the run's range object into the shared rule's input.

    Two named profiles, not one implicit default. A resolved ``QueryScope`` is
    the CALLER profile: the tenant is known up front, so there is nothing to
    discover. A device registration is the DEVICE profile, where ``allowed=None``
    is a *valid* unbound registration — the tenant is discovered from the order
    rows and nothing is blocked.

    A run carries one range object, never both. Passing both is how the standard
    API face used to judge the same tenant twice — once in SQL, once here over
    the rows that SQL had already returned — so it fails fast instead of letting
    one of the two silently win.

    The authorized set must be normalized before it enters ``allowed``: the rule
    normalizes the row tenant before comparing, so two different normalizations
    would read a padded identifier as out of scope. An entry that cannot be
    normalized is not a usable identity and is dropped — the set may then be
    empty, meaning "nothing is visible" — rather than matched as a blank. A
    scope whose tenant cannot be normalized renders the same empty set, which is
    what the SQL push-down renders too (``1=0``): the two renderings of one rule
    cannot disagree about it.
    """
    if scope is not None and allowed_tenants is not None:
        raise ValueError("一次运行只携带一个范围对象：要么传 scope，要么传 allowed_tenants")
    if scope is not None:
        tenant = normalize_tenant(scope.tenant_id)
        return TenantVisibility(
            profile=VisibilityProfile.CALLER,
            allowed=frozenset({tenant}) if tenant else frozenset(),
        )
    if allowed_tenants is None:
        return TenantVisibility(profile=VisibilityProfile.DEVICE, allowed=None)
    return TenantVisibility(
        profile=VisibilityProfile.DEVICE,
        allowed=frozenset(tenant for tenant in map(normalize_tenant, allowed_tenants) if tenant),
    )


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    tool: ToolName
    status: str
    evidence_id: str
    artifact: str
    source: str
    reused: bool = False
    model_payload: Any | None = None

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
        scope: QueryScope | None = None,
        allowed_tenants: set[str] | None = None,
    ) -> None:
        self.sources = sources
        self.request = request
        self.manifest = manifest
        self.journal = journal
        self.safety = safety
        # Tenant learned from order_snapshot discovery; falls back to the
        # request's tenant until the order is located.
        self.effective_tenant: str | None = request.tenant_id
        # The run's tenant visibility, rendered once from the one range object
        # the run was given (see ``_tenant_visibility``).
        self._visibility = _tenant_visibility(scope, allowed_tenants)

    def _tenant(self) -> str | None:
        return self.effective_tenant or self.request.tenant_id

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
        # Discover by order_no without a tenant filter: the tenant is learned
        # from the order row, not pre-bound. This lets an engineer diagnose an
        # order without knowing its tenant. The device path reads its orders
        # over /diag/* HTTP, so there is no SQL to push the scope down into and
        # this is the only place the rule is enforced on that path; on the caller
        # path the scoped source has already pushed it down, and the same rule
        # runs again over what came back. Either way it is enforced by the shared
        # definition and recorded as-is, not re-implemented here — and the record
        # is the one coded result every surface reads (see
        # ``gateway_runtime._blocked_diagnosis_error``).
        rows = self.sources.get_orders(self.request.order_no, None)
        result = visible_orders(rows, self._visibility)
        if result.blocked_tenants:
            # All-or-nothing: one invisible row blocks the whole snapshot, and
            # the discovered tenants are reported instead of silently dropped.
            return self.journal.record(
                tool=ToolName.ORDER_SNAPSHOT,
                source=TENANT_SCOPE_SOURCE,
                status="blocked",
                request=self._identity_request(),
                payload={"orders": [], "discovered_tenant_ids": list(result.blocked_tenants)},
                error=f"订单属于租户 {', '.join(result.blocked_tenants)}，不在授权租户范围内",
            )
        rows = list(result.rows)
        if rows:
            # The discovered tenant is the normalized one — the very value the
            # rule compared — so later tools compare and log the same tenant.
            self.effective_tenant = normalize_tenant(rows[0].get("tenant_id")) or self.effective_tenant
        return self.journal.record(
            tool=ToolName.ORDER_SNAPSHOT,
            source=TOOL_SOURCES[ToolName.ORDER_SNAPSHOT],
            status="success",
            request=self._identity_request(),
            payload={"orders": rows},
            row_count=len(rows),
        )

    def _fee_snapshot(self) -> JournalEntry:
        order, blocked = self._single_order(ToolName.FEE_SNAPSHOT)
        if blocked:
            return blocked
        record = self.sources.get_fee_template_record(
            self.request.order_no,
            order.get("tenant_id"),
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
        runbook_request = DiagnosticRequest(
            order_no=self.request.order_no,
            tenant_id=self._tenant(),
            problem=self.request.problem,
            intent=self.request.intent,
        )
        report = DiagnosticEngine(self.sources, self.safety).diagnose(runbook_request)
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
            "tenant_id": self._tenant(),
        }

    def _outcome(self, tool: ToolName, entry: JournalEntry, *, reused: bool = False) -> ToolOutcome:
        model_payload: Any | None = None
        with contextlib.suppress(ValueError):
            # The journal remains the audit source of truth, but Windows Codex
            # sandboxes may not be able to read the private workspace back.
            model_payload = _bound_model_payload(self.journal.load_payload(entry))
        return ToolOutcome(
            tool=tool,
            status=entry.status,
            evidence_id=entry.evidence_id,
            artifact=entry.artifact,
            source=entry.source,
            reused=reused,
            model_payload=model_payload,
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


def _bound_model_payload(payload: Any, *, max_items: int = 200, max_chars: int = 48_000) -> Any:
    """Keep sanitized evidence usable when the model cannot read workspace files."""

    def bound(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): bound(item) for key, item in value.items()}
        if isinstance(value, list):
            items = [bound(item) for item in value[:max_items]]
            if len(value) > max_items:
                items.append({"_truncated_items": len(value) - max_items})
            return items
        if isinstance(value, str) and len(value) > 4_000:
            return value[:4_000] + "...[truncated]"
        return value

    bounded = bound(payload)
    encoded = json.dumps(bounded, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return bounded
    return {
        "_truncated": True,
        "_notice": "证据内容超过模型上下文上限，请结合 evidence_id 和后续工具结果判断。",
        "preview": encoded[:max_chars],
    }
