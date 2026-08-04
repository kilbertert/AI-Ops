from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from aiops_diagnostics.agent_contracts import AgentDiagnosis
from aiops_diagnostics.models import DiagnosticReport, Severity


def render_report(report: DiagnosticReport, console: Console | None = None) -> None:
    console = console or Console()
    confidence_style = {"high": "green", "medium": "yellow", "low": "red"}.get(report.confidence, "white")
    console.print(
        Panel.fit(
            f"[bold]{escape(report.summary)}[/bold]\n"
            f"订单: {escape(report.request.order_no)}  意图: {escape(report.request.intent.value)}  "
            f"置信度: [{confidence_style}]{report.confidence}[/{confidence_style}]",
            title="AI Ops 只读诊断",
        )
    )

    facts = Table(title="订单事实", show_header=False, box=None)
    facts.add_column("字段", style="cyan", no_wrap=True)
    facts.add_column("值")
    for key, value in report.order_facts.items():
        facts.add_row(escape(key), escape(_format_value(value)))
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
            f"[{style}]{escape(item.severity.value)}[/{style}]",
            escape(item.source),
            escape(item.title),
            escape(item.observation),
        )
    console.print(evidence)

    if report.classifications:
        console.print("[bold]分类:[/bold] " + escape(", ".join(report.classifications)))
    if report.next_steps:
        console.print("\n[bold]建议人工下一步[/bold]")
        for index, step in enumerate(report.next_steps, start=1):
            console.print(f"{index}. {escape(step)}")
    if report.limitations:
        console.print("\n[bold yellow]限制与未确认项[/bold yellow]")
        for item in report.limitations:
            console.print(f"- {escape(str(item))}")
    console.print("\n[dim]已查询: " + escape(", ".join(report.queried_sources)) + "[/dim]")


def render_doctor(result: dict[str, Any], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="数据源只读连接检查")
    table.add_column("数据源")
    table.add_column("状态")
    table.add_column("详情")
    for name, item in result.items():
        ok = bool(item.get("ok"))
        details = item.get("details") if ok else {"error": item.get("error"), "details": item.get("details")}
        table.add_row(
            escape(name),
            "[green]OK[/green]" if ok else "[red]FAIL[/red]",
            escape(_format_value(details)),
        )
    console.print(table)


def render_agent_diagnosis(
    result: AgentDiagnosis,
    run_id: str,
    console: Console | None = None,
) -> None:
    console = console or Console()
    confidence_style = {"high": "green", "medium": "yellow", "low": "red"}.get(
        result.confidence.value,
        "white",
    )
    console.print(
        Panel.fit(
            f"[bold]{escape(result.summary)}[/bold]\n"
            f"订单: {escape(result.order_no)}  状态: {escape(result.status.value)}  "
            f"置信度: [{confidence_style}]{escape(result.confidence.value)}[/{confidence_style}]\n"
            f"运行: {escape(run_id)}",
            title="AI Ops Codex 诊断",
        )
    )
    console.print("[bold]根因结论[/bold]")
    console.print(escape(result.root_cause))
    if result.hypotheses:
        table = Table(title="假设与证据", expand=True)
        table.add_column("假设", width=24)
        table.add_column("解释")
        table.add_column("证据", width=20)
        for hypothesis in result.hypotheses:
            table.add_row(
                escape(hypothesis.title),
                escape(hypothesis.explanation),
                escape(", ".join(hypothesis.evidence_ids)),
            )
        console.print(table)
    if result.limitations:
        console.print("[bold yellow]限制[/bold yellow]")
        for item in result.limitations:
            console.print(f"- {escape(item)}")
    if result.next_steps:
        console.print("[bold]建议人工下一步[/bold]")
        for index, item in enumerate(result.next_steps, start=1):
            console.print(f"{index}. {escape(item)}")


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)
