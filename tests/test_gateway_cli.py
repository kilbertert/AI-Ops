import io

import pytest
from rich.console import Console

from aiops_diagnostics.gateway_cli import _render_run


@pytest.fixture()
def _captured(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_cli.console",
        Console(file=buf, force_terminal=False, color_system=None, width=200),
    )
    return buf


def test_render_run_lists_evidence_and_next_steps(_captured: io.StringIO) -> None:
    run = {
        "run_id": "run-1",
        "status": "diagnosed",
        "confidence": "medium",
        "summary": "金额不一致",
        "result": {
            "root_cause": "服务费计算错误",
            "next_steps": ["核实费率模板", "联系运维确认"],
        },
    }
    evidence = [
        {
            "evidence_id": "ev-001",
            "tool": "order_snapshot",
            "status": "success",
            "row_count": 1,
            "error": None,
        },
        {
            "evidence_id": "ev-002",
            "tool": "fee_snapshot",
            "status": "blocked",
            "row_count": None,
            "error": "必须先请求 order_snapshot",
        },
    ]

    _render_run(run, evidence=evidence)
    out = _captured.getvalue()

    assert "运行 ID: run-1" in out
    assert "状态: diagnosed" in out
    assert "置信度: medium" in out
    assert "摘要: 金额不一致" in out
    assert "根因: 服务费计算错误" in out
    assert "ev-001: order_snapshot -> success，命中 1 行" in out
    assert "ev-002: fee_snapshot -> blocked（必须先请求 order_snapshot）" in out
    assert "下一步建议:" in out
    assert "核实费率模板" in out


def test_render_run_without_evidence_or_result_is_compact(_captured: io.StringIO) -> None:
    _render_run({"run_id": "run-2", "status": "inconclusive", "confidence": "low", "summary": "证据不足"})
    out = _captured.getvalue()
    assert "运行 ID: run-2" in out
    assert "状态: inconclusive" in out
    assert "证据:" not in out
    assert "下一步建议:" not in out
