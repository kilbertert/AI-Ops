"""独立运维入口（`aiops-admin`）：只做 Typer 根应用装配与子命令挂载（ADR-0339）。"""

from __future__ import annotations

import os as os
from pathlib import Path
from typing import Annotated

import typer

from aiops_diagnostics.admin import dify, knowledge, maintenance, onboarding

admin_app = typer.Typer(name="admin", help="AI-Ops 网关管理操作（环境清单收敛）", no_args_is_help=True)


@admin_app.callback()
def configure_admin(
    context: typer.Context,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="使用指定的私有 production.env 配置文件")
    ] = None,
) -> None:
    context.ensure_object(dict)
    if config_file is not None:
        context.obj["config_file"] = config_file


onboarding.register(admin_app)
dify.register(admin_app)
knowledge.register(admin_app)
maintenance.register(admin_app)
