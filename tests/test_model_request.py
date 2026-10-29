"""无工具模型请求的传输、凭据和失败合同。"""

import json

import pytest

from aiops_diagnostics import model_request
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.ops_analysis_narrative import run_narrative_turn


@pytest.mark.parametrize("wire", ["responses", "chat"])
def test_single_request_preserves_schema_and_disables_tools(monkeypatch, wire):
    observed = []

    def request(spec, **kwargs):
        observed.append(spec)
        text = json.dumps({"main_cause": "范围内已观测异常", "recommendation": "请核对设备记录"})
        if wire == "responses":
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            }
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    monkeypatch.setattr(model_request, "request_json", request)
    monkeypatch.setenv("TEST_MODEL_KEY", "test-key")
    provider = ProviderConfig(
        "test", "https://model.example/v1/", wire_api=wire, api_key_env="TEST_MODEL_KEY", model="test-model"
    )
    settings = AgentSettings(providers=(provider,), default_provider="test")
    result = run_narrative_turn({"total": 5, "counts": {"abnormal_ended": 1}}, settings)
    assert result["main_cause"] == "范围内已观测异常"
    assert len(observed) == 1
    spec = observed[0]
    body = json.loads(spec.body)
    assert spec.follow_redirects is False and spec.max_read_bytes == 65536
    assert "tools" not in body and "test-key" not in str(body)
    assert (
        body["text"]["format"]["strict"]
        if wire == "responses"
        else body["response_format"]["json_schema"]["strict"]
    )


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete", "output": []},
        {
            "status": "incomplete",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
        },
        {"status": "completed", "output": [{"type": "function_call"}]},
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
        {"status": "completed", "output": []},
    ],
)
def test_partial_tool_and_refusal_outputs_are_rejected(monkeypatch, response):
    monkeypatch.setattr(model_request, "request_json", lambda *args, **kwargs: response)
    with pytest.raises(ValueError):
        model_request.structured_request(
            provider=ProviderConfig("test", "https://model.example"),
            api_key="test",
            messages=[],
            name="test",
            schema={},
        )


def test_provider_key_keeps_private_file_checks(tmp_path):
    directory = tmp_path / "keys"
    directory.mkdir(mode=0o700)
    path = directory / "test.key"
    path.write_text("test-secret")
    path.chmod(0o600)
    settings = AgentSettings(key_dir=str(directory))
    provider = ProviderConfig("test", "https://model.example")
    assert model_request.resolve_provider_api_key(settings, provider=provider) == "test-secret"
    path.chmod(0o644)
    with pytest.raises(RuntimeError):
        model_request.resolve_provider_api_key(settings, provider=provider)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError):
        model_request.resolve_provider_api_key(settings, provider=provider)


@pytest.mark.parametrize(
    "payload",
    [
        {"main_cause": "缺另一半"},
        {"main_cause": "", "recommendation": "a"},
        {"main_cause": "a", "recommendation": "b", "tools": []},
    ],
)
def test_narrative_must_be_complete(monkeypatch, payload):
    monkeypatch.setenv("AIOPS_CODEX_API_KEY", "test-key")
    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.structured_request", lambda **kwargs: payload
    )
    with pytest.raises(RuntimeError):
        run_narrative_turn({"total": 1}, AgentSettings())
