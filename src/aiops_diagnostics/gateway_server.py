from __future__ import annotations

from typing import Annotated

import typer
import uvicorn
from rich.console import Console

from aiops_diagnostics.console_encoding import configure_windows_stdio
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
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
    settings = GatewayServerSettings.from_env()
    if host:
        settings.bind_host = host
    if port:
        settings.port = port
    settings.validate()
    gateway = create_gateway_app(settings=settings)
    uvicorn.run(
        gateway,
        host=settings.bind_host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
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
