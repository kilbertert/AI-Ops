from pathlib import Path

from rich.console import Console

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.engine import DiagnosticEngine
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.render import render_report
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
