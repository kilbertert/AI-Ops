import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.private_files import validate_private_directory, validate_private_file


def _manifest() -> IncidentManifest:
    return IncidentManifest.from_request(parse_request("订单 TEST-OCPP-0003 金额异常", tenant_id="TENANT-1"))


def test_workspace_is_private_and_stages_references(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]

    workspace = AgentWorkspace.create(project_root, tmp_path, _manifest())

    validate_private_directory(workspace.path)
    validate_private_file(workspace.path / "incident.json")
    if os.name != "nt":
        assert stat.S_IMODE(workspace.path.stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace.path / "incident.json").stat().st_mode) == 0o600
    assert (workspace.path / "references/SOP.md").is_file()
    assert (workspace.path / "references/INDEX.md").is_file()
    assert "causal diagnostic actor" in (workspace.path / "AGENTS.md").read_text()


def test_workspace_stages_and_hashes_fixture_for_resumable_identity(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    source = tmp_path / "source.json"
    source.write_text('{"orders": []}\n', encoding="utf-8")

    workspace = AgentWorkspace.create(project_root, tmp_path / "runs", _manifest(), fixture_path=source)
    staged = workspace.resolve_fixture()
    assert staged == workspace.path / ".inputs/fixture.json"
    assert staged.read_text(encoding="utf-8") == '{"orders": []}\n'

    source.write_text('{"orders": [{"changed": true}]}\n', encoding="utf-8")
    assert workspace.resolve_fixture().read_text(encoding="utf-8") == '{"orders": []}\n'

    staged.write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="已变化"):
        workspace.resolve_fixture()


def test_workspace_rejects_path_escape(tmp_path: Path) -> None:
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, _manifest())

    with pytest.raises(ValueError, match="路径越界"):
        workspace.write_text("../outside.txt", "no")


def test_workspace_rejects_tampered_manifest_identity(tmp_path: Path) -> None:
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, _manifest())
    manifest = json.loads((workspace.path / "incident.json").read_text(encoding="utf-8"))
    manifest["problem"] = "changed after creation"
    (workspace.path / "incident.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="source_hash"):
        workspace.load_manifest()


def test_journal_redacts_sensitive_fields_and_hashes_artifact(tmp_path: Path) -> None:
    manifest = _manifest()
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)

    entry = journal.record(
        tool=ToolName.ORDER_SNAPSHOT,
        source="mysql:ch_order_info",
        status="success",
        request={"order_no": manifest.order_no},
        payload={
            "orders": [
                {
                    "order_no": manifest.order_no,
                    "vin": "LSV12345678901234",
                    "password": "database-secret",
                    "id": "internal-order-id",
                    "out_trade_no": "merchant-payment-reference",
                    "decoded": "phone 13800138000 vin LSV12345678901234 plate 京A12345",
                    "txSerialNo": "123456789012345678",
                }
            ]
        },
        row_count=1,
    )

    artifact = json.loads((workspace.path / entry.artifact).read_text())
    rendered = json.dumps(artifact, ensure_ascii=False)
    assert manifest.order_no in rendered
    assert "LSV12345678901234" not in rendered
    assert "database-secret" not in rendered
    assert "internal-order-id" not in rendered
    assert "merchant-payment-reference" not in rendered
    assert "13800138000" not in rendered
    assert "京A12345" not in rendered
    assert "123456789012345678" in rendered
    assert entry.artifact_sha256
    assert journal.get(entry.evidence_id) == entry

    (workspace.path / entry.artifact).write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="哈希不匹配"):
        journal.load_payload(entry)
