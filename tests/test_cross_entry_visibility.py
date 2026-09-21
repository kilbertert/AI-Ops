"""Cross-entry equivalence: both profiles must reach the same conclusion (#325 T7).

The two source sets coexist (``HybridSources`` over ``/diag/*`` HTTP for device
runs, ``ScopedSources`` pushing SQL for caller runs), so the same order could be
judged by two different mechanisms. The convergence point is therefore the *rule*
rather than the execution point: whichever entry a run comes through, the shared
preparation orchestration renders one range object into one shared profile and
records one coded result.

These tests drive that preparation orchestration twice with the same order and the
same tenant constraint — once as a device registration (``allowed_tenants``), once
as a resolved caller scope (``scope``) — and assert that the visibility conclusion
and the blocked reason are identical. That equivalence is what the six independent
implementations never had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_runner import run_agent_diagnosis
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import CodexTurnOutput
from aiops_diagnostics.config import AgentSettings, Settings
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.order_visibility import TENANT_SCOPE_SOURCE
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.query_scope import QueryScope

ORDER_NO = "TEST-OCPP-0003"
ORDER_TENANT = "TENANT-DEMO"
PROJECT_ROOT = Path(__file__).parents[1]


class _ScriptedSession:
    """One tool batch for the order snapshot, then a diagnosis turn.

    The diagnosis reads what the tools actually recorded, so the run reaches a
    conclusion either way instead of stalling on an invalid one. The assertions
    here are about the evidence journal, not about the model.
    """

    def __init__(self, workspace, _settings, *, provider=None, thread_id=None) -> None:
        self.workspace = workspace
        self.manifest = workspace.load_manifest()
        self.turns = 0

    @property
    def thread_id(self) -> str:
        return "scripted-thread"

    def set_progress_callback(self, _callback) -> None:
        return None

    def run(self, prompt: str) -> CodexTurnOutput:
        del prompt
        self.turns += 1
        if self.turns == 1:
            response: dict[str, Any] = {
                "kind": "tool_requests",
                "tool_requests": [{"tool": ToolName.ORDER_SNAPSHOT.value, "reason": "定位订单"}],
                "diagnosis": None,
            }
            return CodexTurnOutput(turn_id="turn-1", final_response=json.dumps(response), usage={})

        entries = EvidenceJournal(self.workspace, self.manifest).entries()
        successful = [entry.evidence_id for entry in entries if entry.status == "success"]
        blocked = [entry.error for entry in entries if entry.status == "blocked" and entry.error]
        if successful:
            status, confidence = "diagnosed", "medium"
        else:
            status, confidence = "blocked", "low"
        response = {
            "kind": "diagnosis",
            "tool_requests": [],
            "diagnosis": {
                "schema_version": "1.0",
                "incident_id": self.manifest.incident_id,
                "order_no": self.manifest.order_no,
                "tenant_id": self.manifest.tenant_id,
                "status": status,
                "summary": "跨入口等价测试",
                "root_cause": "共享租户可见性规则的同一结论",
                "confidence": confidence,
                "evidence_ids": successful,
                "hypotheses": [],
                "limitations": blocked,
                "failed_sources": [],
                "next_steps": [],
            },
        }
        return CodexTurnOutput(turn_id="turn-2", final_response=json.dumps(response), usage={})

    def close(self) -> None:
        return None


def _fixture(tmp_path: Path, order_tenant: str) -> Path:
    path = tmp_path / "orders.json"
    path.write_text(
        json.dumps({"orders": [{"order_no": ORDER_NO, "tenant_id": order_tenant}]}),
        encoding="utf-8",
    )
    return path


def _run_entry(
    run_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: Path,
    tenant: str,
    *,
    allowed_tenants: set[str] | None,
    scope: QueryScope | None,
) -> list[dict[str, Any]]:
    """Run one entry's preparation orchestration; return its order evidence."""
    request = parse_request("订单金额异常", order_no=ORDER_NO, tenant_id=tenant)
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(PROJECT_ROOT, run_root, manifest, fixture_path=fixture)
    settings = Settings()
    settings.agent = AgentSettings(codex_bin="/bin/true", max_turns=3)
    monkeypatch.setenv("AIOPS_CODEX_API_KEY", "test-key")
    monkeypatch.setattr("aiops_diagnostics.agent_engine.SDKCodexSession", _ScriptedSession)

    run_agent_diagnosis(
        workspace,
        request,
        settings,
        fixture,
        allowed_tenants=allowed_tenants,
        scope=scope,
    )

    journal = EvidenceJournal(workspace, manifest)
    return [
        {
            "status": entry.status,
            "source": entry.source,
            "error": entry.error,
            "payload": journal.load_payload(entry),
        }
        for entry in journal.entries()
        if entry.tool == ToolName.ORDER_SNAPSHOT.value
    ]


def _device_entry(
    run_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: Path,
    tenant: str,
    allowed: set[str] | None,
) -> list[dict[str, Any]]:
    return _run_entry(run_root, monkeypatch, fixture, tenant, allowed_tenants=allowed, scope=None)


def _caller_entry(
    run_root: Path, monkeypatch: pytest.MonkeyPatch, fixture: Path, tenant: str
) -> list[dict[str, Any]]:
    return _run_entry(
        run_root,
        monkeypatch,
        fixture,
        tenant,
        allowed_tenants=None,
        scope=QueryScope(tenant_id=tenant, site_ids=None, user_id=None),
    )


@pytest.mark.parametrize(
    ("tenant", "visible"),
    [
        # In scope: the order is located on both entries.
        (ORDER_TENANT, True),
        # The same tenant, padded: one normalization, so both entries agree.
        (f" {ORDER_TENANT} ", True),
        # Out of scope: blocked on both entries, with the same coded reason.
        ("tenant-elsewhere", False),
    ],
)
def test_both_entries_reach_the_same_visibility_conclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenant: str, visible: bool
) -> None:
    """One order, one tenant constraint, two profiles, one conclusion.

    The device entry names the tenant through ``allowed_tenants`` (the DEVICE
    profile, which may also bind nothing and discover it); the caller entry names
    it through a frozen ``QueryScope`` (the CALLER profile, where the tenant is
    already known). Both go through the same preparation orchestration, so the
    recorded evidence — status, source, error, visible rows — must match.
    """
    fixture = _fixture(tmp_path, ORDER_TENANT)
    device = _device_entry(tmp_path / "device", monkeypatch, fixture, tenant, {tenant})
    caller = _caller_entry(tmp_path / "caller", monkeypatch, fixture, tenant.strip())

    assert len(device) == len(caller) == 1
    device_order, caller_order = device[0], caller[0]
    assert device_order["status"] == caller_order["status"]
    assert device_order["source"] == caller_order["source"]
    assert device_order["error"] == caller_order["error"]

    if visible:
        assert device_order["status"] == "success"
        assert [row["order_no"] for row in device_order["payload"]["orders"]] == [ORDER_NO]
    else:
        # The one coded reason every surface reads: same source, same reported
        # tenant, same all-or-nothing block, and the order never leaks through.
        assert device_order["source"] == TENANT_SCOPE_SOURCE
        assert device_order["payload"]["orders"] == []
        assert device_order["payload"]["discovered_tenant_ids"] == [ORDER_TENANT]


def test_an_unbound_registration_sees_what_a_resolved_scope_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace-level registration and a resolved scope locate the same order.

    The device profile's ``allowed=None`` is a *valid* unbound registration: the
    tenant is discovered from the order row and nothing is blocked. The caller
    profile knows the tenant up front. Both must land on the same order — which is
    what keeps the workspace-level registration design deliberate rather than an
    accident of ``allowed_tenants=None``.
    """
    fixture = _fixture(tmp_path, ORDER_TENANT)
    device = _device_entry(tmp_path / "device", monkeypatch, fixture, ORDER_TENANT, None)
    caller = _caller_entry(tmp_path / "caller", monkeypatch, fixture, ORDER_TENANT)

    assert [entry["status"] for entry in device] == ["success"]
    assert [entry["status"] for entry in caller] == ["success"]
    assert [row["order_no"] for row in device[0]["payload"]["orders"]] == [ORDER_NO]
    assert [row["order_no"] for row in caller[0]["payload"]["orders"]] == [ORDER_NO]


# --- the docs must keep describing both source sets ---------------------------------
#
# The two source sets coexist because the device path has no SQL to push into, not
# because there are two rules. Both the env template and the architecture doc used
# to name only one of them — the env template called restricted-direct "the default
# diagnostic path", which is false for device runs — so the reader of either one
# could not tell why there are two tenant mechanisms at all. Naming only one is how
# a reader concludes the other is dead code and deletes it.


def test_the_env_template_names_both_source_sets() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "默认诊断路径已改为受限直连（ScopedSources）" in text
    # The correction that keeps the deprecated section from reading as pure history:
    # the device run path still reads its orders over /diag/* HTTP.
    assert "设备运行路径（POST /v1/runs）" in text
    assert "HybridSources" in text
    assert "order_visibility.py" in text


def test_the_architecture_doc_names_both_source_sets() -> None:
    text = (PROJECT_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "两个源集合并存" in text
    assert "HybridSources" in text
    assert "ScopedSources" in text
    assert "order_visibility.py" in text


@pytest.mark.parametrize("name", [".env.example", "docs/architecture.md"])
def test_the_doc_correction_names_the_real_standard_api_route(name: str) -> None:
    """The doc-drift fix must not introduce a new path drift (#334).

    This ticket exists to correct documentation drift, and the correction it
    added named the standard API face ``/v1/diagnoses`` — a route this repository
    does not serve (the real one is ``/v1/standard/diagnoses``). A reader who
    follows the corrected sentence lands on a 404, which is the same class of
    drift the ticket was opened to remove.
    """
    text = (PROJECT_ROOT / name).read_text(encoding="utf-8")

    assert "/v1/standard/diagnoses" in text
    assert "/v1/diagnoses" not in text.replace("/v1/standard/diagnoses", "")
