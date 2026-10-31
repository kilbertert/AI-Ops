"""验收失败后，取日志超时不能跳过隔离栈清理或丢失原失败。"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("failed_step", ["logs", "down"])
def test_diagnostic_timeout_preserves_failure_and_attempts_cleanup(tmp_path, monkeypatch, failed_step):
    path = Path(__file__).resolve().parents[2] / "tools/verification/verify_consumer_chatflow.py"
    spec = importlib.util.spec_from_file_location("consumer_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DIFY_TEST_NAMESPACE", "synthetic-cleanup")
    monkeypatch.setenv("DIFY_TEST_ROOT", str(tmp_path))
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [str(path), "--out", str(report)])
    calls = []

    def compose(env, *args, **kwargs):
        calls.append(args[0])
        if args[0] == failed_step:
            raise subprocess.TimeoutExpired(["docker", args[0]], kwargs["timeout"])
        return subprocess.CompletedProcess(args, 1 if args[0] == "up" else 0, "", "")

    monkeypatch.setattr(module.stack, "_compose", compose)
    assert module.main() == 1
    result = json.loads(report.read_text())
    assert calls == ["up", "logs", "down"]
    assert result["error"] == {"type": "AssertionError", "message": "dify ready"}
    assert result["cleanup_ok"] is (failed_step != "down")
    assert result["cleanup_errors"] == [{"step": failed_step, "type": "TimeoutExpired"}]
