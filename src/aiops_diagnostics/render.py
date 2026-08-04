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
    *,
    events_path: str | None = None,
    evidence_journal_path: str | None = None,
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
    if events_path or evidence_journal_path:
        console.print("[dim]追溯文件[/dim]")
        if events_path:
            console.print(f"[dim]事件日志: {escape(events_path)}[/dim]")
        if evidence_journal_path:
            console.print(f"[dim]证据日志: {escape(evidence_journal_path)}[/dim]")


def render_progress_event(event: dict[str, Any], console: Console | None = None) -> None:
    """Render one persisted runtime event without exposing evidence payloads."""
    console = console or Console()
    event_type = str(event.get("type", "runtime_event"))
    timestamp = str(event.get("at", ""))
    stamp = timestamp[11:19] if len(timestamp) >= 19 else timestamp
    turn_id = str(event.get("turn_id", ""))
    if event_type == "diagnosis_started":
        message = f"开始诊断 {event.get('run_id', '')}".rstrip()
        if event.get("events_path"):
            message += f"；事件日志 {event['events_path']}"
    elif event_type == "codex_session_starting":
        message = "正在连接 Codex provider"
    elif event_type == "codex_thread_ready":
        message = "Codex thread 已就绪"
    elif event_type == "codex_turn_waiting":
        message = f"等待 Codex 第 {event.get('turn_number', '?')}/{event.get('max_turns', '?')} 轮"
    elif event_type == "codex_turn_started":
        message = f"Codex turn 已开始 {turn_id}"
    elif event_type == "codex_turn_heartbeat":
        message = f"心跳: Codex turn {turn_id} 已运行 {event.get('elapsed_seconds', '?')} 秒"
    elif event_type == "codex_turn_completed":
        message = f"Codex turn 已完成 {turn_id}"
    elif event_type == "codex_turn_timeout":
        message = f"Codex turn 超时 {turn_id}"
    elif event_type == "codex_turn_failed":
        message = f"Codex turn 失败 {turn_id}"
    elif event_type == "tool_batch_started":
        tools = event.get("tools", [])
        labels = [str(item) for item in tools] if isinstance(tools, list) else []
        message = "开始执行证据工具: " + (", ".join(labels) or "无")
    elif event_type == "tool_batch_completed":
        outcomes = event.get("outcomes", [])
        labels = [
            f"{item.get('tool', '?')}={item.get('status', '?')}"
            for item in outcomes
            if isinstance(item, dict)
        ]
        message = "证据工具完成: " + (", ".join(labels) or "无")
    elif event_type == "diagnosis_validation_failed":
        message = f"结果合同校验未通过，将请求第 {event.get('attempt', '?')} 次修复"
    elif event_type == "diagnosis_completed":
        message = f"诊断完成: {event.get('status', '?')}"
    elif event_type == "diagnosis_blocked":
        message = "诊断被阻断，保留当前证据与事件日志"
    elif event_type == "diagnosis_interrupted":
        message = "诊断中断，可使用相同 run 恢复"
    else:
        message = event_type
    prefix = f"[dim]{escape(stamp)}[/dim] " if stamp else ""
    console.print(prefix + escape(message))


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)
