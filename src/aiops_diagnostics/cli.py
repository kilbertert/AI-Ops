from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from aiops_diagnostics.config import Settings
from aiops_diagnostics.engine import DiagnosticEngine
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.render import render_doctor, render_report
from aiops_diagnostics.sources import FixtureSources, SourceError, live_sources

app = typer.Typer(
    name="aiops",
    help="充电订单全链路只读诊断运行时",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()


@app.command()
def diagnose(
    problem: Annotated[str, typer.Argument(help="用户反馈，例如：订单 123 金额异常")],
    order_no: Annotated[str | None, typer.Option("--order-no", help="明确指定订单号")] = None,
    tenant_id: Annotated[str | None, typer.Option("--tenant-id", help="跨租户环境建议明确指定")] = None,
    fixture: Annotated[Path | None, typer.Option("--fixture", exists=True, dir_okay=False)] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 报告")] = False,
) -> None:
    """Diagnose one order without executing any business-side action."""
    try:
        request = parse_request(problem, order_no=order_no, tenant_id=tenant_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = _load_settings()
    if fixture:
        report = DiagnosticEngine(FixtureSources(fixture), settings.safety).diagnose(request)
    else:
        try:
            with live_sources(settings) as sources:
                report = DiagnosticEngine(sources, settings.safety).diagnose(request)
        except (SourceError, ValueError) as exc:
            console.print(f"[bold red]初始化失败:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
    if as_json:
        console.print_json(report.to_json())
    else:
        render_report(report, console)


@app.command()
def doctor(
    show_config: Annotated[bool, typer.Option("--show-config", help="显示脱敏后的配置")] = False,
) -> None:
    """Check read-only connectivity and required tables/streams."""
    settings = _load_settings()
    if show_config:
        console.print_json(data=settings.redacted())
    try:
        with live_sources(settings) as sources:
            render_doctor(sources.doctor(), console)
    except (SourceError, ValueError) as exc:
        console.print(f"[bold red]连接检查失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@app.command("shell")
def interactive_shell(
    tenant_id: Annotated[str | None, typer.Option("--tenant-id", help="会话默认租户")] = None,
    fixture: Annotated[Path | None, typer.Option("--fixture", exists=True, dir_okay=False)] = None,
) -> None:
    """Start a Codex-like prompt loop for repeated read-only diagnoses."""
    settings = _load_settings()
    console.print("[bold]AI Ops 只读诊断 Shell[/bold]，输入 exit 或 quit 退出。")
    if fixture:
        _shell_loop(DiagnosticEngine(FixtureSources(fixture), settings.safety), tenant_id)
        return
    try:
        with live_sources(settings) as sources:
            _shell_loop(DiagnosticEngine(sources, settings.safety), tenant_id)
    except (SourceError, ValueError) as exc:
        console.print(f"[bold red]初始化失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _shell_loop(engine: DiagnosticEngine, tenant_id: str | None) -> None:
    while True:
        try:
            text = console.input("\n[bold cyan]aiops>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if text.lower() in {"exit", "quit", "退出"}:
            return
        if not text:
            continue
        try:
            request = parse_request(text, tenant_id=tenant_id)
        except ValueError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            continue
        render_report(engine.diagnose(request), console)


def _load_settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        console.print(f"[bold red]配置无效:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
