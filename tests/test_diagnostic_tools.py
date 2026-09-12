from pathlib import Path

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources, TDengineSource

FIXTURE = Path(__file__).parents[1] / "examples/fixtures/ocpp_consistent.json"


def _executor(
    tmp_path: Path,
    *,
    allowed_tenants: set[str] | None = None,
) -> tuple[DiagnosticToolExecutor, EvidenceJournal]:
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    return (
        DiagnosticToolExecutor(
            FixtureSources(FIXTURE),
            request,
            manifest,
            journal,
            safety=SafetySettings(),
            allowed_tenants=allowed_tenants,
        ),
        journal,
    )


def test_codex_selected_tools_enforce_dependencies_and_reuse_evidence(tmp_path: Path) -> None:
    executor, journal = _executor(tmp_path)

    blocked = executor.execute(ToolName.GUN_TIMESERIES)
    order = executor.execute(ToolName.ORDER_SNAPSHOT)
    gun = executor.execute(ToolName.GUN_TIMESERIES)
    reused = executor.execute(ToolName.ORDER_SNAPSHOT)

    assert blocked.status == "blocked"
    assert order.status == "success"
    assert gun.status == "success"
    assert reused.reused is True
    assert reused.evidence_id == order.evidence_id
    assert order.model_payload["orders"]  # type: ignore[index]
    assert reused.model_payload == order.model_payload
    assert "model_payload" not in order.to_dict()
    assert "payload" not in order.to_dict()
    assert len(journal.entries()) == 3
    payload = journal.load_payload(journal.get(gun.evidence_id))  # type: ignore[arg-type]
    assert "samples" in payload


def test_known_runbook_is_optional_advisory_evidence(tmp_path: Path) -> None:
    executor, journal = _executor(tmp_path)

    blocked = executor.execute(ToolName.KNOWN_RUNBOOK)
    executor.execute(ToolName.ORDER_SNAPSHOT)
    outcome = executor.execute(ToolName.KNOWN_RUNBOOK)

    payload = journal.load_payload(journal.get(outcome.evidence_id))  # type: ignore[arg-type]
    assert blocked.status == "blocked"
    assert outcome.status == "success"
    assert payload["report"]["request"]["order_no"] == "TEST-OCPP-0003"


def test_order_scoped_tools_require_a_unique_order_snapshot(tmp_path: Path) -> None:
    executor, _ = _executor(tmp_path)

    assert executor.execute(ToolName.FEE_SNAPSHOT).status == "blocked"
    assert executor.execute(ToolName.REDIS_SYNC).status == "blocked"
    executor.execute(ToolName.ORDER_SNAPSHOT)
    assert executor.execute(ToolName.FEE_SNAPSHOT).status == "success"
    assert executor.execute(ToolName.REDIS_SYNC).status == "success"


def test_order_snapshot_blocks_when_tenant_outside_authorized_scope(tmp_path: Path) -> None:
    # The fixture order belongs to TENANT-DEMO; the device is scoped to tenant-a.
    executor, journal = _executor(tmp_path, allowed_tenants={"tenant-a"})

    outcome = executor.execute(ToolName.ORDER_SNAPSHOT)

    assert outcome.status == "blocked"
    entry = journal.get(outcome.evidence_id)
    assert entry is not None
    assert entry.source == "harness:tenant_scope"
    assert "TENANT-DEMO" in (entry.error or "")
    payload = journal.load_payload(entry)
    assert payload["discovered_tenant_ids"] == ["TENANT-DEMO"]
    # A blocked order_snapshot must not populate the effective tenant.
    assert executor.effective_tenant is None


def test_order_snapshot_discovers_tenant_within_authorized_scope(tmp_path: Path) -> None:
    executor, _ = _executor(tmp_path, allowed_tenants={"TENANT-DEMO"})

    outcome = executor.execute(ToolName.ORDER_SNAPSHOT)

    assert outcome.status == "success"
    # The discovered tenant is learned from the order, not pre-bound.
    assert executor.effective_tenant == "TENANT-DEMO"


class _DoctorSources(FixtureSources):
    """带可脚本化 doctor() 的替身；其他行为继承 FixtureSources。"""

    def __init__(self, fixture: Path, doctor_report: dict) -> None:
        super().__init__(fixture)
        self.doctor_report = doctor_report

    def doctor(self) -> dict:
        if isinstance(self.doctor_report, Exception):
            raise self.doctor_report
        return self.doctor_report


def _journal_for(tmp_path: Path) -> tuple[object, EvidenceJournal]:
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    return request, journal


def test_preflight_records_blocked_entries_and_notes(tmp_path: Path) -> None:
    """doctor 报告 stable/列缺失时，预检记 blocked 证据并返回同名注记。"""
    from aiops_diagnostics.diagnostic_tools import preflight_environment

    request, journal = _journal_for(tmp_path)
    sources = _DoctorSources(
        FIXTURE,
        {
            "tdengine": {
                "ok": True,
                "details": {
                    "charging_gun_property": False,
                    "charging_pile_comm": False,
                    "gun_columns": {},
                },
            },
        },
    )
    notes = preflight_environment(sources, request, journal)
    assert len(notes) == 2
    assert "charging-gun_property 表不存在" in notes[0]
    assert "charging-pile_comm 表不存在" in notes[1]
    entries = journal.entries()
    blocked = [entry for entry in entries if entry.status == "blocked"]
    assert {entry.tool for entry in blocked} == {"gun_timeseries", "comm_messages"}
    assert all("预检" in (entry.error or "") for entry in blocked)
    # blocked 不等于 failed：不进入 failed_sources（validator 声明要求不被触发）
    assert journal.failed_sources() == []


def test_preflight_column_gap_blocks_gun_only(tmp_path: Path) -> None:
    """stable 存在但缺列（41 形态）时，只封 gun_timeseries 且点名缺列。"""
    from aiops_diagnostics.diagnostic_tools import preflight_environment

    request, journal = _journal_for(tmp_path)
    columns = {name: True for name in TDengineSource.GUN_COLUMNS}
    columns["batteryMinTemperature"] = False
    sources = _DoctorSources(
        FIXTURE,
        {
            "tdengine": {
                "ok": True,
                "details": {
                    "charging_gun_property": True,
                    "charging_pile_comm": True,
                    "gun_columns": columns,
                },
            },
        },
    )
    notes = preflight_environment(sources, request, journal)
    assert len(notes) == 1
    assert "batteryMinTemperature" in notes[0]
    assert [entry.tool for entry in journal.entries() if entry.status == "blocked"] == ["gun_timeseries"]


def test_preflight_tolerates_doctor_failure(tmp_path: Path) -> None:
    """doctor 抛错时预检静默退化为无注记，绝不破坏诊断。"""
    from aiops_diagnostics.diagnostic_tools import preflight_environment

    request, journal = _journal_for(tmp_path)
    sources = _DoctorSources(FIXTURE, RuntimeError("doctor down"))
    assert preflight_environment(sources, request, journal) == ()
    assert journal.entries() == []


def test_preflight_absent_when_sources_lack_doctor(tmp_path: Path) -> None:
    """FixtureSources 没有 doctor —— 预检返回空（保持既有行为）。"""
    from aiops_diagnostics.diagnostic_tools import preflight_environment

    request, journal = _journal_for(tmp_path)
    assert preflight_environment(FixtureSources(FIXTURE), request, journal) == ()


def test_execute_still_runs_after_preflight(tmp_path: Path) -> None:
    """预检不短路：模型仍请求该工具时真实执行并自然失败（真实 failed 条目）。"""
    from aiops_diagnostics.diagnostic_tools import preflight_environment

    request, journal = _journal_for(tmp_path)
    columns = {name: True for name in TDengineSource.GUN_COLUMNS}
    columns["batteryMinTemperature"] = False
    sources = _DoctorSources(
        FIXTURE,
        {
            "tdengine": {
                "ok": True,
                "details": {
                    "charging_gun_property": True,
                    "charging_pile_comm": True,
                    "gun_columns": columns,
                },
            },
        },
    )
    preflight_environment(sources, request, journal)
    manifest = _current_manifest()
    executor = DiagnosticToolExecutor(sources, request, manifest, journal, safety=SafetySettings())
    outcome = executor.execute(ToolName.ORDER_SNAPSHOT)
    assert outcome.status == "success", "不相关工具照常执行"


def _current_manifest() -> IncidentManifest:
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    return IncidentManifest.from_request(request)
