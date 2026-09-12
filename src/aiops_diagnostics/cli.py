from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.agent_contracts import (
    AgentDiagnosis,
    DiagnosisStatus,
    IncidentManifest,
    RunState,
)
from aiops_diagnostics.agent_engine import ProgressCallback
from aiops_diagnostics.agent_runner import run_agent_diagnosis
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import (
    AgentRuntimeError,
    prepare_runtime_home,
    provider_key_fingerprint,
    resolve_provider_api_key,
)
from aiops_diagnostics.config import (
    AgentSettings,
    ProviderConfig,
    Settings,
    canonical_provider_base_url,
    require_same_provider_base_url,
    selected_config_file,
    validate_key_slot_name,
)
from aiops_diagnostics.console_encoding import configure_windows_stdio
from aiops_diagnostics.engine import DiagnosticEngine
from aiops_diagnostics.gateway_cli import remote_app
from aiops_diagnostics.models import DiagnosticRequest, Intent
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.platform_paths import (
    data_root,
    default_codex_home,
    default_key_dir,
    default_run_root,
    reference_root,
)
from aiops_diagnostics.private_files import (
    PrivatePathError,
    ensure_private_directory,
    write_private_text,
)
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.render import (
    render_agent_diagnosis,
    render_doctor,
    render_progress_event,
    render_report,
)
from aiops_diagnostics.sources import FixtureSources, SourceError, live_sources, scoped_live_sources

configure_windows_stdio()

app = typer.Typer(
    name="aiops",
    help="充电订单全链路只读诊断运行时",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()
_CONFIG_FILE_OVERRIDE: Path | None = None
app.add_typer(remote_app, name="remote")
app.add_typer(admin_app, name="admin")


class DiagnoseMode(StrEnum):
    agent = "agent"
    deterministic = "deterministic"


@app.callback()
def configure_cli(
    context: typer.Context,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="使用指定的私有 production.env 配置文件"),
    ] = None,
) -> None:
    global _CONFIG_FILE_OVERRIDE
    context.ensure_object(dict)
    context.obj["config_file"] = config_file
    _CONFIG_FILE_OVERRIDE = config_file


@app.command("init")
def init_runtime(
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="明确覆盖现有配置模板，不会覆盖 key slot"),
    ] = False,
) -> None:
    """Initialize platform-native private configuration and runtime directories."""
    config_path = _selected_config_path()
    template = reference_root() / ".env.example"
    try:
        ensure_private_directory(config_path.parent)
        ensure_private_directory(default_key_dir())
        ensure_private_directory(default_codex_home())
        ensure_private_directory(default_run_root())
        if config_path.exists() and not overwrite:
            console.print(f"配置已存在，未覆盖: {config_path}")
        else:
            write_private_text(config_path, template.read_text(encoding="utf-8"))
            console.print(f"已创建私有配置模板: {config_path}")
    except (OSError, PrivatePathError, FileNotFoundError) as exc:
        console.print(f"[bold red]初始化失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print_json(
        data={
            "config_file": str(config_path),
            "key_dir": str(default_key_dir()),
            "codex_home": str(default_codex_home()),
            "run_root": str(default_run_root()),
        }
    )


@app.command("key-install")
def key_install(
    slot: Annotated[str, typer.Argument(help="密钥槽名称，例如 primary 或 backup")],
    source_file: Annotated[
        Path | None,
        typer.Option("--from-file", exists=True, dir_okay=False, help="从文件读取密钥，避免命令行明文"),
    ] = None,
    replace_existing: Annotated[
        bool,
        typer.Option("--replace", help="明确替换同名密钥槽"),
    ] = False,
) -> None:
    """Install one provider key into the platform-native private slot store."""
    try:
        validate_key_slot_name(slot)
        settings = _load_settings()
        key_dir = ensure_private_directory(Path(settings.agent.key_dir).expanduser().resolve())
        target = key_dir / f"{slot}.key"
        if target.exists() and not replace_existing:
            raise ValueError(f"密钥槽已存在，使用 --replace 才能替换: {slot}")
        if source_file is not None:
            if source_file.is_symlink():
                raise ValueError("密钥来源文件不得是符号链接")
            key = source_file.read_text(encoding="utf-8").strip()
        else:
            key = typer.prompt("API key", hide_input=True, confirmation_prompt=True).strip()
        if not key:
            raise ValueError("API key 不能为空")
        write_private_text(target, key + "\n")
    except (OSError, PrivatePathError, ValueError) as exc:
        console.print(f"[bold red]密钥安装失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print_json(
        data={
            "ok": True,
            "key_slot": slot,
            "key_fingerprint": provider_key_fingerprint(key),
            "key_dir": str(key_dir),
        }
    )


@app.command("paths")
def show_paths() -> None:
    """Show resolved local paths without exposing credentials."""
    settings = _load_settings()
    console.print_json(
        data={
            "platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
            "config_file": str(_selected_config_path()),
            "data_root": str(data_root()),
            "key_dir": settings.agent.key_dir,
            "codex_home": settings.agent.codex_runtime_home,
            "run_root": settings.agent.run_root,
            "codex_bin": settings.agent.codex_bin,
            "ssh_bin": settings.ssh.ssh_bin,
        }
    )


def _diagnose_agent(
    request: DiagnosticRequest,
    settings: Settings,
    fixture: Path | None,
    key_slot: str | None,
    as_json: bool,
    progress: bool,
    provider: str | None = None,
    *,
    scope: QueryScope | None = None,
) -> None:
    """Run the Codex-native, evidence-journaled read-only diagnosis."""
    try:
        selected_provider = settings.agent.select_provider(provider)
    except ValueError as exc:
        console.print(f"[bold red]Codex 诊断中断:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    effective_key_slot = key_slot or selected_provider.resolved_key_slot()
    settings.agent.key_slot = effective_key_slot
    _validate_agent_settings(settings)
    try:
        resolve_provider_api_key(settings.agent, provider=selected_provider, key_slot=effective_key_slot)
    except (AgentRuntimeError, ValueError, OSError) as exc:
        console.print(f"[bold red]Codex 诊断中断:[/bold red] {exc}")
        console.print(
            "[yellow]提示：未配置 provider key；离线 fixture 或回归诊断可改用 --mode deterministic。[/yellow]"
        )
        raise typer.Exit(code=2) from exc
    manifest = IncidentManifest.from_request(request)
    project_root = _project_root()
    run_root = _run_root(settings.agent.run_root)
    workspace: AgentWorkspace | None = None
    try:
        workspace = AgentWorkspace.create(
            project_root,
            run_root,
            manifest,
            fixture_path=fixture,
            provider_base_url=canonical_provider_base_url(selected_provider.base_url),
            provider=selected_provider.name,
            key_slot=effective_key_slot,
        )
        resolved_fixture = workspace.resolve_fixture()
        result = _run_agent(
            workspace,
            request,
            settings,
            resolved_fixture,
            provider=selected_provider.name,
            key_slot=effective_key_slot,
            progress_callback=_progress_callback(progress, as_json),
            scope=scope,
        )
    except (AgentRuntimeError, SourceError, ValueError, OSError) as exc:
        console.print(f"[bold red]Codex 诊断中断:[/bold red] {exc}")
        if workspace is not None:
            console.print(f"运行 ID: {workspace.run_id}，可使用 agent-resume 恢复")
        raise typer.Exit(code=2) from exc
    assert workspace is not None
    _render_agent_output(result, workspace, as_json)


def _diagnose_deterministic(
    request: DiagnosticRequest,
    settings: Settings,
    fixture: Path | None,
    as_json: bool,
    *,
    scope: QueryScope | None = None,
) -> None:
    """Run the deterministic rule-engine diagnosis (offline fixtures, regression)."""
    if fixture:
        try:
            report = DiagnosticEngine(FixtureSources(fixture), settings.safety).diagnose(request)
        except (SourceError, ValueError, OSError) as exc:
            console.print(f"[bold red]初始化失败:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
    else:
        try:
            if scope is not None:
                with scoped_live_sources(settings, scope=scope) as sources:
                    report = DiagnosticEngine(sources, settings.safety).diagnose(request)
            else:
                with live_sources(settings) as sources:
                    report = DiagnosticEngine(sources, settings.safety).diagnose(request)
        except (SourceError, ValueError) as exc:
            console.print(f"[bold red]初始化失败:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
    if as_json:
        console.print_json(report.to_json())
    else:
        render_report(report, console)


def _parse_scope_json(value: str | None) -> QueryScope | None:
    """解析受权限约束的诊断范围 JSON（QueryScope）。

    由已认证运维入口透传；本工具不信任前端裸传的权限字段，解析失败即拒绝。
    """
    if value is None or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--scope-json 不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("--scope-json 必须是对象")
    tenant_raw = payload.get("tenant_id")
    if not isinstance(tenant_raw, str) or not tenant_raw.strip():
        raise typer.BadParameter("--scope-json 缺少有效 tenant_id")
    site_raw = payload.get("site_ids")
    if site_raw is None:
        sites: tuple[str, ...] | None = None
    elif isinstance(site_raw, list) and all(isinstance(item, str) for item in site_raw):
        sites = tuple(site_raw)
    else:
        raise typer.BadParameter("--scope-json site_ids 必须是字符串数组或 null")
    user_raw = payload.get("user_id")
    if user_raw is not None and not isinstance(user_raw, str):
        raise typer.BadParameter("--scope-json user_id 必须是字符串或 null")
    return QueryScope(tenant_id=tenant_raw.strip(), site_ids=sites, user_id=user_raw)


@app.command("agent-resume")
def agent_resume(
    run_id: Annotated[str, typer.Argument(help="需要恢复的 run ID")],
    key_slot: Annotated[
        str | None,
        typer.Option("--key-slot", help="恢复时切换到同一 base_url 下的备用密钥槽"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 诊断合同")] = False,
    progress: Annotated[
        bool,
        typer.Option("--progress/--no-progress", help="显示运行中的日志与心跳；JSON 结果写入 stdout"),
    ] = True,
) -> None:
    """Resume the same Codex thread and immutable incident workspace."""
    settings = _load_settings()
    workspace = AgentWorkspace.open(_run_root(settings.agent.run_root), run_id)
    manifest = workspace.load_manifest()
    state = workspace.load_state()
    try:
        selected_provider = _resume_provider(settings.agent, state)
        require_same_provider_base_url(selected_provider.base_url, state.provider_base_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    effective_key_slot = key_slot or state.key_slot
    settings.agent.key_slot = effective_key_slot
    _validate_agent_settings(settings)
    if effective_key_slot != state.key_slot:
        try:
            resolve_provider_api_key(settings.agent, provider=selected_provider, key_slot=effective_key_slot)
        except (AgentRuntimeError, ValueError, OSError) as exc:
            console.print(f"[bold red]Codex runtime 无效:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
        workspace.save_state(state.model_copy(update={"key_slot": effective_key_slot}))
    request = DiagnosticRequest(
        order_no=manifest.order_no,
        tenant_id=manifest.tenant_id,
        problem=manifest.problem,
        intent=Intent(manifest.intent),
    )
    try:
        fixture = workspace.resolve_fixture()
        result = _run_agent(
            workspace,
            request,
            settings,
            fixture,
            provider=selected_provider.name,
            key_slot=effective_key_slot,
            progress_callback=_progress_callback(progress, as_json),
        )
    except (AgentRuntimeError, SourceError, ValueError, OSError) as exc:
        console.print(f"[bold red]Codex 恢复失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    _render_agent_output(result, workspace, as_json)


@app.command("agent-doctor")
def agent_doctor(
    key_slot: Annotated[
        str | None,
        typer.Option("--key-slot", help="检查指定 API 密钥槽"),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="检查指定 model provider"),
    ] = None,
) -> None:
    """Verify the isolated Codex runtime without exposing authentication data."""
    settings = _load_settings()
    try:
        selected_provider = settings.agent.select_provider(provider)
    except ValueError as exc:
        console.print(f"[bold red]Codex runtime 无效:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    effective_key_slot = key_slot or selected_provider.resolved_key_slot()
    settings.agent.key_slot = effective_key_slot
    try:
        provider_key = resolve_provider_api_key(
            settings.agent, provider=selected_provider, key_slot=effective_key_slot
        )
        runtime_home = prepare_runtime_home(settings.agent, provider=selected_provider)
    except (AgentRuntimeError, ValueError, OSError) as exc:
        console.print(f"[bold red]Codex runtime 无效:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print_json(
        data={
            "ok": True,
            "codex_bin": str(Path(settings.agent.codex_bin).expanduser()),
            "runtime_home": str(runtime_home),
            "provider": selected_provider.name,
            "base_url": selected_provider.base_url,
            "model": selected_provider.model,
            "wire_api": selected_provider.wire_api,
            "key_slot": effective_key_slot,
            "key_fingerprint": provider_key_fingerprint(provider_key),
            "permission_profile": "aiops-diagnostic",
            "network": "disabled for model-generated commands",
            "business_mutations": "disabled",
            "windows_sandbox": settings.agent.windows_sandbox,
        }
    )


@app.command()
def diagnose(
    problem: Annotated[str, typer.Argument(help="用户反馈，例如：订单 123 金额异常")],
    order_no: Annotated[str | None, typer.Option("--order-no", help="明确指定订单号")] = None,
    tenant_id: Annotated[str | None, typer.Option("--tenant-id", help="跨租户环境建议明确指定")] = None,
    fixture: Annotated[Path | None, typer.Option("--fixture", exists=True, dir_okay=False)] = None,
    mode: Annotated[
        DiagnoseMode,
        typer.Option(
            "--mode", help="诊断模式：agent=Codex 原生（默认）；deterministic=确定性规则，离线/回归"
        ),
    ] = DiagnoseMode.agent,
    key_slot: Annotated[
        str | None,
        typer.Option("--key-slot", help="agent 模式选择同一 API base_url 下的密钥槽"),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="agent 模式选择 model provider，默认使用配置中的默认 provider"),
    ] = None,
    scope_json: Annotated[
        str | None,
        typer.Option(
            "--scope-json",
            help="受权限约束的诊断范围 JSON（QueryScope）："
            ' {"tenant_id":"T", "site_ids":["S1"], "user_id":null}。'
            " 由已认证运维入口透传，本工具不信任前端裸传权限字段",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 报告或诊断合同")] = False,
    progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress", help="agent 模式显示运行中的日志与心跳；JSON 结果写入 stdout"
        ),
    ] = True,
) -> None:
    """Diagnose one order without executing any business-side action.

    默认走 Codex-native agent 路径；加 --mode deterministic 走确定性规则路径（离线 fixture、CI 回归）。
    """
    try:
        request = parse_request(problem, order_no=order_no, tenant_id=tenant_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = _load_settings()
    scope = _parse_scope_json(scope_json)
    if mode is DiagnoseMode.agent:
        _diagnose_agent(request, settings, fixture, key_slot, as_json, progress, provider, scope=scope)
    else:
        _diagnose_deterministic(request, settings, fixture, as_json, scope=scope)


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
        try:
            _shell_loop(DiagnosticEngine(FixtureSources(fixture), settings.safety), tenant_id)
        except (SourceError, ValueError, OSError) as exc:
            console.print(f"[bold red]初始化失败:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
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
        return Settings.from_config(_config_file_override())
    except ValueError as exc:
        console.print(f"[bold red]配置无效:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _run_agent(
    workspace: AgentWorkspace,
    request: DiagnosticRequest,
    settings: Settings,
    fixture: Path | None,
    *,
    provider: str | None = None,
    key_slot: str | None = None,
    progress_callback: ProgressCallback | None = None,
    scope: QueryScope | None = None,
) -> AgentDiagnosis:
    return run_agent_diagnosis(
        workspace,
        request,
        settings,
        fixture,
        provider=provider,
        key_slot=key_slot,
        progress_callback=progress_callback,
        scope=scope,
    )


def _render_agent_output(result: AgentDiagnosis, workspace: AgentWorkspace, as_json: bool) -> None:
    events_path = str(workspace.path / "events.jsonl")
    evidence_journal_path = str(workspace.path / "evidence-journal.jsonl")
    if as_json:
        console.print_json(
            data={
                "run_id": workspace.run_id,
                "diagnosis": result.model_dump(mode="json"),
                "events_path": events_path,
                "evidence_journal_path": evidence_journal_path,
            }
        )
    else:
        render_agent_diagnosis(
            result,
            workspace.run_id,
            console,
            events_path=events_path,
            evidence_journal_path=evidence_journal_path,
        )
    if result.status == DiagnosisStatus.INCONCLUSIVE:
        raise typer.Exit(code=3)
    if result.status == DiagnosisStatus.BLOCKED:
        raise typer.Exit(code=4)


def _progress_callback(enabled: bool, as_json: bool) -> ProgressCallback | None:
    if not enabled:
        return None
    progress_console = Console(stderr=as_json)

    def callback(event: dict[str, object]) -> None:
        render_progress_event(event, progress_console)

    return callback


def _project_root() -> Path:
    return reference_root()


def _run_root(configured: str) -> Path:
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _config_file_override() -> Path | None:
    return _CONFIG_FILE_OVERRIDE


def _selected_config_path() -> Path:
    return selected_config_file(_config_file_override())


def _validate_agent_settings(settings: Settings) -> None:
    try:
        settings.agent.validate()
    except ValueError as exc:
        console.print(f"[bold red]Codex 配置无效:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _resume_provider(agent_settings: AgentSettings, state: RunState) -> ProviderConfig:
    """Select the provider a run was started on, preserving the redirect invariant.

    Runs created before the provider field existed (``state.provider`` empty) are
    matched against the registered providers by base_url; if none match, the
    default provider is returned and the caller's base_url comparison rejects a
    silent redirect to a different host.
    """
    if state.provider:
        return agent_settings.select_provider(state.provider)
    for candidate in agent_settings.providers:
        if canonical_provider_base_url(candidate.base_url) == canonical_provider_base_url(
            state.provider_base_url
        ):
            return candidate
    return agent_settings.select_provider(None)
