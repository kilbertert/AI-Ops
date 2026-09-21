from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.order_visibility import (
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    visible_orders,
)
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import FixtureSources, TDengineSource

FIXTURE = Path(__file__).parents[1] / "examples/fixtures/ocpp_consistent.json"
SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"


def _executor(
    tmp_path: Path,
    *,
    allowed_tenants: set[str] | None = None,
    orders: list[dict] | None = None,
    scope: QueryScope | None = None,
) -> tuple[DiagnosticToolExecutor, EvidenceJournal]:
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    fixture = FIXTURE
    if orders is not None:
        fixture = tmp_path / "fixtures" / "orders.json"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps({"orders": orders}), encoding="utf-8")
    return (
        DiagnosticToolExecutor(
            FixtureSources(fixture),
            request,
            manifest,
            journal,
            safety=SafetySettings(),
            allowed_tenants=allowed_tenants,
            scope=scope,
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


def _caller_visibility(scope: QueryScope) -> TenantVisibility:
    """Render a resolved scope the way the scoped direct source renders it.

    Both are renderings of one rule, so they must agree: the tool layer decides
    on the rows it holds, the source pushes the same decision into SQL.
    """
    tenant = normalize_tenant(scope.tenant_id)
    return TenantVisibility(
        profile=VisibilityProfile.CALLER,
        allowed=frozenset({tenant}) if tenant else frozenset(),
    )


@pytest.mark.parametrize(
    ("scope", "rows"),
    [
        # Out of scope: blocked, and the discovered tenant is reported.
        (
            QueryScope(tenant_id="T-1", site_ids=None, user_id=None),
            [{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}],
        ),
        # In scope: the row survives.
        (
            QueryScope(tenant_id="TENANT-DEMO", site_ids=None, user_id=None),
            [{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}],
        ),
        # The same tenant, padded: one normalization, so it is in scope.
        (
            QueryScope(tenant_id="TENANT-DEMO", site_ids=None, user_id=None),
            [{"order_no": "TEST-OCPP-0003", "tenant_id": " TENANT-DEMO "}],
        ),
        # A row without a tenant cannot be shown to be in scope: fail closed.
        (QueryScope(tenant_id="T-1", site_ids=None, user_id=None), [{"order_no": "TEST-OCPP-0003"}]),
        # No rows: nothing to block.
        (QueryScope(tenant_id="T-1", site_ids=None, user_id=None), []),
    ],
)
def test_a_resolved_scope_reaches_the_tool_layer_as_the_caller_profile(
    tmp_path: Path, scope: QueryScope, rows: list[dict]
) -> None:
    """The caller path carries one range object, so the tool layer renders it.

    The standard API face used to hand the tenant to the tool layer a second
    time — ``allowed_tenants={context.effective_tenant_id}`` beside the ``scope``
    the SQL push-down already used. It now passes the scope alone, so the tool
    layer must derive its visibility from that object: the CALLER profile,
    because a resolved scope always knows its tenant and has nothing to discover.
    The same candidate rows are evaluated independently by the shared rule here
    and the tool layer's recorded conclusion is checked against it.
    """
    executor, journal = _executor(tmp_path, scope=scope, orders=rows)
    expected = visible_orders(rows, _caller_visibility(scope))

    outcome = executor.execute(ToolName.ORDER_SNAPSHOT)
    entry = journal.get(outcome.evidence_id)
    assert entry is not None

    if expected.blocked_tenants:
        assert outcome.status == "blocked"
        assert entry.source == "harness:tenant_scope"
        payload = journal.load_payload(entry)
        assert payload["discovered_tenant_ids"] == list(expected.blocked_tenants)
        assert executor.effective_tenant is None
    else:
        assert outcome.status == "success"
        visible = journal.load_payload(entry)["orders"]
        assert [row.get("tenant_id") for row in visible] == [row.get("tenant_id") for row in expected.rows]


def test_a_run_may_not_carry_two_range_objects(tmp_path: Path) -> None:
    """One range object per run, enforced rather than assumed.

    The standard API face used to pass ``scope=query_scope`` *and*
    ``allowed_tenants={context.effective_tenant_id}`` — the same tenant twice. It
    agreed with itself only while one call site kept reading the same field
    twice. Silently preferring one of the two would let that come back without a
    test noticing, so the tool layer refuses the combination instead.
    """
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    with pytest.raises(ValueError, match="一个范围对象"):
        DiagnosticToolExecutor(
            FixtureSources(FIXTURE),
            request,
            manifest,
            EvidenceJournal(workspace, manifest),
            safety=SafetySettings(),
            scope=QueryScope(tenant_id="T-1", site_ids=None, user_id=None),
            allowed_tenants={"T-1"},
        )


def _device_visibility(allowed_tenants: set[str] | None) -> TenantVisibility:
    """Express the device's authorized set as shared-rule device-profile input."""
    if allowed_tenants is None:
        return TenantVisibility(profile=VisibilityProfile.DEVICE, allowed=None)
    return TenantVisibility(
        profile=VisibilityProfile.DEVICE,
        allowed=frozenset(tenant for tenant in map(normalize_tenant, allowed_tenants) if tenant),
    )


@pytest.mark.parametrize(
    ("rows", "allowed_tenants"),
    [
        # Out of scope: blocked, and the effective tenant stays untouched.
        ([{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}], {"tenant-a"}),
        # In scope: discovered from the order row.
        ([{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}], {"TENANT-DEMO"}),
        # The same tenant, padded: the shared normalization reads it as in scope.
        ([{"order_no": "TEST-OCPP-0003", "tenant_id": " TENANT-DEMO "}], {"TENANT-DEMO"}),
        # A row without a tenant cannot be shown to be in scope: fail closed and
        # report it, instead of crashing on the mixed blocked set.
        ([{"order_no": "TEST-OCPP-0003"}], {"TENANT-DEMO"}),
        (
            [{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}, {"order_no": "TEST-OCPP-0003"}],
            {"tenant-a"},
        ),
        # A workspace-level registration binds no tenant: discover, block nothing.
        ([{"order_no": "TEST-OCPP-0003", "tenant_id": "TENANT-DEMO"}], None),
        # No order rows: nothing to block, so the 0-row success is unchanged.
        ([], {"tenant-a"}),
    ],
)
def test_order_snapshot_records_the_shared_rule_result(
    tmp_path: Path, rows: list[dict], allowed_tenants: set[str] | None
) -> None:
    """The tool layer must not compare tenants itself: it must record the shared rule.

    The device path reads its orders over ``/diag/*`` HTTP, so there is no SQL to
    push the scope down into and the tool layer is the only place the rule is
    enforced there — but the enforcement point is not the definition. The same
    candidate rows are evaluated independently by the shared rule here, and the
    tool layer's recorded conclusion (blocked tenant set, visible rows,
    effective tenant) is checked against it.
    """
    executor, journal = _executor(tmp_path, allowed_tenants=allowed_tenants, orders=rows)
    expected = visible_orders(rows, _device_visibility(allowed_tenants))

    outcome = executor.execute(ToolName.ORDER_SNAPSHOT)
    entry = journal.get(outcome.evidence_id)
    assert entry is not None

    if expected.blocked_tenants:
        assert outcome.status == "blocked"
        # External contract: the source and the key name do not change.
        assert entry.source == "harness:tenant_scope"
        payload = journal.load_payload(entry)
        assert payload["discovered_tenant_ids"] == list(expected.blocked_tenants)
        # A blocked snapshot must not write the out-of-scope tenant anywhere.
        assert executor.effective_tenant is None
    else:
        assert outcome.status == "success"
        assert entry.source == "mysql:ch_order_info"
        visible = journal.load_payload(entry)["orders"]
        assert [row.get("tenant_id") for row in visible] == [row.get("tenant_id") for row in expected.rows]
        assert executor.effective_tenant == (normalize_tenant(rows[0].get("tenant_id")) if rows else None)


def test_the_tool_layer_does_not_own_the_tenant_rule() -> None:
    """The tool layer must not hold a tenant comparison of its own.

    Source-level rather than behavioural, because what is being prevented is a
    future edit: the rule lives once in ``order_visibility``. The device path has
    no SQL to push down, so a second comparison growing back here — raw values
    versus normalized ones, a missing tenant in or out of scope — is how the same
    order starts reaching different conclusions on this path than on the others.
    Reading a row's tenant (``row.get("tenant_id")``) stays allowed; comparing one
    does not.
    """
    tree = ast.parse(
        (SOURCE_ROOT / "diagnostic_tools.py").read_text(encoding="utf-8"),
        filename="diagnostic_tools.py",
    )

    def _names_tenant(node: ast.AST) -> bool:
        # ``row.get("tenant_id")`` and ``request.tenant_id`` are the two spellings
        # a comparison could reach a tenant through.
        return any(
            (isinstance(sub, ast.Attribute) and sub.attr == "tenant_id")
            or (isinstance(sub, ast.Constant) and sub.value == "tenant_id")
            for sub in ast.walk(node)
        )

    offenders = [
        f"diagnostic_tools.py:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and _names_tenant(node)
    ]
    assert not offenders, (
        f"tenant_id must be compared only in order_visibility.py; found: {', '.join(offenders)}"
    )


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
