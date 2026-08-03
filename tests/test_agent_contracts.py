import pytest
from pydantic import ValidationError

from aiops_diagnostics.agent_contracts import AgentTurn, IncidentManifest
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.redaction import redact_text


def test_incident_manifest_is_stable_and_redacts_labeled_pii() -> None:
    request = parse_request(
        "订单 TEST-OCPP-0003 金额异常，手机号 13800138000",
        tenant_id="TENANT-1",
    )

    first = IncidentManifest.from_request(request)
    second = IncidentManifest.from_request(request)

    assert first == second
    assert first.incident_id.startswith("incident-")
    assert first.order_no == "TEST-OCPP-0003"
    assert "13800138000" not in first.problem
    assert "REDACTED" in first.problem


def test_incident_manifest_preserves_order_but_redacts_unlabeled_long_identifiers() -> None:
    request = parse_request(
        "订单 TEST-OCPP-0003 关联账号 123456789012345678 和 VIN LSV12345678901234",
    )

    manifest = IncidentManifest.from_request(request)

    assert manifest.order_no in manifest.problem
    assert "123456789012345678" not in manifest.problem
    assert "LSV12345678901234" not in manifest.problem


def test_agent_turn_requires_payload_matching_kind() -> None:
    with pytest.raises(ValidationError, match="tool_requests turn"):
        AgentTurn.model_validate({"kind": "tool_requests", "tool_requests": []})

    with pytest.raises(ValidationError, match="diagnosis turn"):
        AgentTurn.model_validate(
            {
                "kind": "diagnosis",
                "tool_requests": [{"tool": "order_snapshot", "reason": "inspect"}],
            }
        )


def test_free_text_redaction_removes_bearer_and_openai_style_keys() -> None:
    api_key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    rendered = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz and " + api_key)

    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "Bearer REDACTED" in rendered
