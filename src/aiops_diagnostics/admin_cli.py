"""``aiops admin`` — declarative gateway management commands.

Currently one command: ``admin reconcile`` converges the gateway agent store
toward an ``ops/environments/<env>.toml`` manifest through AgentManager (the
production code path). See :mod:`aiops_diagnostics.agent_manifest`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from aiops_diagnostics.config import Settings, selected_config_file
from aiops_diagnostics.gateway_config import GatewayServerSettings

admin_app = typer.Typer(name="admin", help="AI-Ops 网关管理操作（环境清单收敛）", no_args_is_help=True)


def _reconcile_settings(config_file: Path | None) -> Settings:
    path = config_file if config_file is not None else selected_config_file()
    return Settings.from_config(path)


@admin_app.command("reconcile")
def reconcile(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="环境清单 TOML")],
    db: Annotated[
        Path | None,
        typer.Option("--db", help="gateway 数据库文件（默认取 Gateway 环境配置解析）"),
    ] = None,
    kb_url: Annotated[
        str | None,
        typer.Option(
            "--kb-url",
            envvar="AIOPS_GATEWAY_KB_SERVICE_BASE_URL",
            help="kb-service 地址（带 KB 绑定时发布校验需要）",
        ),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option("--prune/--no-prune", help="禁用/删除清单租户内清单外的 agent"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只输出计划，零写入")] = False,
) -> None:
    """按环境清单收敛 gateway agent（幂等；走 AgentManager 生产代码路径）。"""
    from aiops_diagnostics.agent_debug import KbBindingResolver, KbServiceKnowledgeClient
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import ManifestError, load_manifest
    from aiops_diagnostics.agent_manifest import reconcile as reconcile_manifest

    try:
        environment = load_manifest(manifest)
    except ManifestError as exc:
        typer.secho(f"清单无效: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    database = db or GatewayServerSettings.from_env().database_file

    # 与 gateway_api.create_gateway_app 相同的 allowed_models 推导
    agent_settings = settings.agent
    configured_models = tuple(provider.model for provider in agent_settings.providers if provider.model)
    if not configured_models:
        configured_models = (agent_settings.model or "aiops-api",)

    knowledge_resolver = None
    if kb_url:
        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(kb_url, tenant_id="aiops", timeout=10)
        )
    manager = AgentManager(
        AgentStore(database),
        knowledge_resolver=knowledge_resolver,
        allowed_models=configured_models or ("aiops-api",),
    )

    try:
        reports: list[Any] = reconcile_manifest(manager, environment, prune=prune, dry_run=dry_run)
    except ManifestError as exc:
        typer.secho(f"收敛中止（库未变更）: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        json.dumps(
            {"dry_run": dry_run, "reports": [dataclasses.asdict(report) for report in reports]},
            ensure_ascii=False,
            indent=2,
        )
    )
