from pathlib import Path

from rich.console import Console

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.engine import DiagnosticEngine
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.render import render_progress_event, render_report
from aiops_diagnostics.sources import FixtureSources


def test_render_escapes_dynamic_rich_markup() -> None:
    fixture = Path(__file__).parents[1] / "examples" / "fixtures" / "ocpp_consistent.json"
    report = DiagnosticEngine(FixtureSources(fixture), SafetySettings()).diagnose(
        parse_request("订单 TEST-OCPP-0003 金额是否正常")
    )
    report.limitations.append("driver returned [broken] tag")
    report.evidence[0].observation = "literal [bold]payload[/bold]"
    console = Console(record=True, force_terminal=False, width=200)

    render_report(report, console)

    output = console.export_text()
    assert "driver returned [broken] tag" in output
    assert "literal [bold]payload[/bold]" in output


def test_render_progress_event_shows_heartbeat_without_payload() -> None:
    console = Console(record=True, force_terminal=False, width=120)

    render_progress_event(
        {
            "at": "2026-08-04T08:00:01+00:00",
            "type": "codex_turn_heartbeat",
            "turn_id": "turn-1",
            "elapsed_seconds": 10,
            "payload": {"secret": "must not be rendered"},
        },
        console,
    )

    output = console.export_text()
    assert "心跳" in output
    assert "turn-1" in output
    assert "must not be rendered" not in output
