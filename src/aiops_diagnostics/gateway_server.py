from __future__ import annotations

from typing import Annotated

import typer
import uvicorn
from rich.console import Console

from aiops_diagnostics.console_encoding import configure_windows_stdio
from aiops_diagnostics.deployment_environment import require_deployment_identity
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_logging import configure_gateway_logging
from aiops_diagnostics.gateway_store import GatewayStore

configure_windows_stdio()

app = typer.Typer(
    name="aiops-gateway",
    help="AI-Ops central read-only diagnostic gateway",
    no_args_is_help=True,
)
console = Console()


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option("--host", help="覆盖监听地址")] = None,
    port: Annotated[int | None, typer.Option("--port", help="覆盖监听端口")] = None,
) -> None:
    # 隔离门禁先于一切：设置 `M2LIVE_ISOLATION_ROOT` 的运行必须证明自己的可写状态、
    # 只读依赖与属主都在隔离根里，否则在打开任何库之前退出。未设置时为空操作 ——
    # 生产 Gateway 的启动路径逐字不变。
    from aiops_diagnostics.isolation_guard import IsolationError, enforce_from_env

    try:
        enforce_from_env()
    except IsolationError as exc:
        console.print(f"[red]isolation refused: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    settings = GatewayServerSettings.from_env()
    if host:
        settings.bind_host = host
    if port:
        settings.port = port
    settings.validate()
    # 部署身份**必填、无默认值**（#879 / ADR-0059）。`validate()` 只判格式，因为一个直接
    # 构造出来的 settings 不是一次部署；**起监听**才是一次部署，所以必备性在这里判。
    # 判据只有一处实现（`require_deployment_identity`）—— M2 的两个 launcher 用的是同一个。
    try:
        require_deployment_identity(settings.environment)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    # After validate(): a bad log level is a startup error like any other bad
    # setting, and it must fail before the port is taken rather than after.
    configure_gateway_logging(settings.log_level)
    gateway = create_gateway_app(settings=settings)
    # `log_config=None` keeps uvicorn from calling `dictConfig` over ours —
    # Python's documented recipe for "configure logging yourself, then hand
    # control to uvicorn". Without it uvicorn's config replaces the root
    # handler we just installed and the drop comes straight back.
    uvicorn.run(
        gateway,
        host=settings.bind_host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )


@app.command("issue-enrollment")
def issue_enrollment(
    workspace_id: Annotated[str, typer.Option("--workspace", help="跨设备共享的工作区标识")],
    tenant_id: Annotated[str | None, typer.Option("--tenant-id", help="将设备固定到租户")] = None,
    ttl_seconds: Annotated[
        int,
        typer.Option("--ttl-seconds", min=60, max=86_400, help="一次性注册码有效期"),
    ] = 600,
) -> None:
    settings = GatewayServerSettings.from_env()
    store = GatewayStore(settings.database_file)
    code = store.issue_enrollment(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        expires_in_seconds=ttl_seconds,
    )
    console.print_json(
        data={
            "enrollment_code": code,
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "expires_in_seconds": ttl_seconds,
        }
    )


@app.command("devices")
def list_devices(
    workspace_id: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    settings = GatewayServerSettings.from_env()
    store = GatewayStore(settings.database_file)
    console.print_json(data={"devices": store.list_devices(workspace_id)})


@app.command("revoke-device")
def revoke_device(device_id: str) -> None:
    settings = GatewayServerSettings.from_env()
    store = GatewayStore(settings.database_file)
    if not store.revoke_device(device_id):
        console.print(f"[yellow]设备不存在或已撤销: {device_id}[/yellow]")
        raise typer.Exit(code=2)
    console.print_json(data={"ok": True, "device_id": device_id})


def main() -> None:
    app()


if __name__ == "__main__":
    main()
