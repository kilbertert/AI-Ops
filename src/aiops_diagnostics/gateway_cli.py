from __future__ import annotations

import platform
import socket
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from aiops_diagnostics.gateway_client import (
    GatewayClient,
    GatewayClientError,
    client_from_profile,
    enroll_profile,
)
from aiops_diagnostics.gateway_config import canonical_gateway_url
from aiops_diagnostics.gateway_tokens import delete_token, load_profile
from aiops_diagnostics.render import render_progress_event

remote_app = typer.Typer(
    name="remote",
    help="连接中央 AI-Ops Gateway，跨设备运行和查看只读诊断",
    no_args_is_help=True,
)
console = Console()


@remote_app.command("enroll")
def enroll(
    url: Annotated[str, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")],
    profile: Annotated[str, typer.Option("--profile", help="本机 Gateway 配置名称")] = "default",
    device_name: Annotated[str | None, typer.Option("--device-name")] = None,
    code_file: Annotated[
        Path | None,
        typer.Option("--code-file", exists=True, dir_okay=False, help="从私有文件读取一次性注册码"),
    ] = None,
) -> None:
    code = (
        code_file.read_text(encoding="utf-8").strip()
        if code_file
        else typer.prompt("一次性注册码", hide_input=True)
    )
    try:
        enrolled, storage = enroll_profile(
            url,
            code,
            profile_name=profile,
            device_name=device_name or socket.gethostname(),
            platform=platform.system().lower(),
        )
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 注册失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print_json(
        data={
            "ok": True,
            "profile": enrolled.name,
            "base_url": enrolled.base_url,
            "device_id": enrolled.device_id,
            "workspace_id": enrolled.workspace_id,
            "token_store": storage,
        }
    )


@remote_app.command("doctor")
def doctor(
    url: Annotated[str | None, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")] = None,
    profile: Annotated[str, typer.Option("--profile")] = "default",
) -> None:
    try:
        client = _client(profile, url)
        console.print_json(data=client.health())
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 检查失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@remote_app.command("diagnose")
def diagnose(
    problem: Annotated[str, typer.Argument(help="用户反馈，例如：订单 123 金额异常")],
    order_no: Annotated[str | None, typer.Option("--order-no")] = None,
    tenant_id: Annotated[str | None, typer.Option("--tenant-id")] = None,
    key_slot: Annotated[str | None, typer.Option("--key-slot")] = None,
    fixture: Annotated[
        str | None, typer.Option("--fixture", help="仅接受 Gateway 白名单 fixture 名称")
    ] = None,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    url: Annotated[str | None, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        client = _client(profile, url)
        run = client.create_run(
            problem=problem,
            order_no=order_no,
            tenant_id=tenant_id,
            key_slot=key_slot,
            fixture_name=fixture,
        )
        if wait:
            progress_console = Console(stderr=as_json)

            def on_event(event: dict[str, object]) -> None:
                if progress:
                    render_progress_event(event, progress_console)

            run = client.wait_for_run(run["run_id"], on_event=on_event)
        if as_json:
            console.print_json(data=run)
        else:
            _render_run(run)
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 诊断失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@remote_app.command("runs")
def runs(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    url: Annotated[str | None, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
) -> None:
    try:
        client = _client(profile, url)
        console.print_json(data={"runs": client.list_runs(limit=limit)})
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 查询失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@remote_app.command("show")
def show(
    run_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    url: Annotated[str | None, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")] = None,
) -> None:
    try:
        console.print_json(data=_client(profile, url).get_run(run_id))
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 查询失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@remote_app.command("events")
def events(
    run_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    url: Annotated[str | None, typer.Option("--url", envvar="AIOPS_GATEWAY_URL")] = None,
    after: Annotated[int, typer.Option("--after", min=0)] = 0,
) -> None:
    try:
        console.print_json(data=_client(profile, url).list_events(run_id, after=after))
    except (GatewayClientError, OSError, ValueError) as exc:
        console.print(f"[bold red]Gateway 查询失败:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


@remote_app.command("logout")
def logout(profile: Annotated[str, typer.Option("--profile")] = "default") -> None:
    delete_token(profile)
    try:
        path = load_profile(profile).file_path
    except (FileNotFoundError, ValueError):
        path = None
    if path is not None:
        path.unlink(missing_ok=True)
    console.print_json(data={"ok": True, "profile": profile})


def _client(profile_name: str, url: str | None) -> GatewayClient:
    if url:
        return GatewayClient(canonical_gateway_url(url), _token_for_profile(profile_name))
    return client_from_profile(load_profile(profile_name))


def _token_for_profile(profile_name: str) -> str:
    from aiops_diagnostics.gateway_tokens import load_token

    return load_token(profile_name)


def _render_run(run: dict[str, object]) -> None:
    console.print(f"运行 ID: {run.get('run_id', '')}")
    console.print(f"状态: {run.get('status', '')}")
    if run.get("confidence"):
        console.print(f"置信度: {run['confidence']}")
    if run.get("summary"):
        console.print(f"摘要: {run['summary']}")
