from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aiops_diagnostics.models import DiagnosticReport, Severity


def render_report(report: DiagnosticReport, console: Console | None = None) -> None:
    console = console or Console()
    confidence_style = {"high": "green", "medium": "yellow", "low": "red"}.get(report.confidence, "white")
    console.print(
        Panel.fit(
            f"[bold]{report.summary}[/bold]\n"
            f"订单: {report.request.order_no}  意图: {report.request.intent.value}  "
            f"置信度: [{confidence_style}]{report.confidence}[/{confidence_style}]",
            title="AI Ops 只读诊断",
        )
    )

    facts = Table(title="订单事实", show_header=False, box=None)
    facts.add_column("字段", style="cyan", no_wrap=True)
    facts.add_column("值")
    for key, value in report.order_facts.items():
        facts.add_row(key, _format_value(value))
    console.print(facts)

    evidence = Table(title="证据链", expand=True)
    evidence.add_column("级别", width=8)
    evidence.add_column("数据源", width=24)
    evidence.add_column("检查项", width=24)
    evidence.add_column("观察")
    for item in report.evidence:
        style = {
            Severity.INFO: "green",
            Severity.WARNING: "yellow",
            Severity.CRITICAL: "bold red",
        }[item.severity]
        evidence.add_row(
            f"[{style}]{item.severity.value}[/{style}]", item.source, item.title, item.observation
        )
    console.print(evidence)

    if report.classifications:
        console.print("[bold]分类:[/bold] " + ", ".join(report.classifications))
    if report.next_steps:
        console.print("\n[bold]建议人工下一步[/bold]")
        for index, step in enumerate(report.next_steps, start=1):
            console.print(f"{index}. {step}")
    if report.limitations:
        console.print("\n[bold yellow]限制与未确认项[/bold yellow]")
        for item in report.limitations:
            console.print(f"- {item}")
    console.print("\n[dim]已查询: " + ", ".join(report.queried_sources) + "[/dim]")


def render_doctor(result: dict[str, Any], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="数据源只读连接检查")
    table.add_column("数据源")
    table.add_column("状态")
    table.add_column("详情")
    for name, item in result.items():
        ok = bool(item.get("ok"))
        details = item.get("details") if ok else {"error": item.get("error"), "details": item.get("details")}
        table.add_row(name, "[green]OK[/green]" if ok else "[red]FAIL[/red]", _format_value(details))
    console.print(table)


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)
