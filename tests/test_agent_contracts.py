import pytest
from pydantic import ValidationError

from aiops_diagnostics.agent_contracts import AgentTurn, IncidentManifest, agent_turn_schema
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


def test_agent_turn_schema_is_strict_for_responses_api() -> None:
    schema = agent_turn_schema()

    assert schema["required"] == ["kind", "tool_requests", "diagnosis"]
    assert schema["additionalProperties"] is False
    _assert_strict_objects(schema)


def test_the_two_fields_the_model_kept_failing_are_documented_in_the_schema() -> None:
    """模型只看得到序列化后的 schema —— 类型注解里的道理它一个字都看不到。

    41 上实测（2026-10-22，44/63 个 run 卡在重试上限 2）最贵的两次合约返工：
    ``hypotheses[].evidence_ids`` 缺失，和 ``failed_sources`` 里写了「源码 + 解释」。
    两者都改在了校验器/收口上，但如果 schema 里仍然一个字都没有，模型只能靠撞墙
    学会它们 —— 而这正是那两轮时间的来源。这条用例钉住「说明确实下发了」，
    不是钉住措辞：断言的是模型必须知道的那两件事。
    """
    definitions = agent_turn_schema()["$defs"]

    hypothesis_evidence = definitions["Hypothesis"]["properties"]["evidence_ids"]["description"]
    # 必须说明「失败/阻塞的条目也算」，否则模型会以为只有成功证据合法。
    assert "failed" in hypothesis_evidence and "known_runbook" in hypothesis_evidence

    failed_sources = definitions["AgentDiagnosis"]["properties"]["failed_sources"]["description"]
    # 必须说明「照抄 source 标识符」，这是那对自相矛盾报错的根源。
    assert "source" in failed_sources and "invent" in failed_sources


def test_free_text_redaction_removes_bearer_and_openai_style_keys() -> None:
    api_key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    rendered = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz and " + api_key)

    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "Bearer REDACTED" in rendered


def _assert_strict_objects(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _assert_strict_objects(item)
        return
    if not isinstance(node, dict):
        return

    assert "default" not in node
    properties = node.get("properties")
    if isinstance(properties, dict):
        assert node.get("additionalProperties") is False
        assert node.get("required") == list(properties)
    for value in node.values():
        _assert_strict_objects(value)
