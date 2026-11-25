"""运维 CLI 共用配置解析、控制台凭据合并与辅助函数。"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import typer

from aiops_diagnostics.common.config import Settings, selected_config_file
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_config import SETTING_SOURCES, GatewayServerSettings

_SETTING_FOR = {source.env: name for name, source in SETTING_SOURCES.items() if source.file}


def _reconcile_settings(config_file: Path | None) -> Settings:
    return Settings.from_config(selected_config_file(config_file))


def _dify_knowledge_registry(console: Any) -> Any:
    """The operator's ``knowledge_id → (tenant, our kb ids)`` table, parsed once.

    The **same** parser the inbound retrieval route uses
    (``parse_dify_knowledge_bindings``); the pull reads it in the other
    direction. Sharing the parser is the point — two parsers for one table is
    the "one rule, two implementations" defect this repo keeps re-learning.
    """
    from aiops_diagnostics.gateway.gateway_config import parse_dify_knowledge_bindings

    return parse_dify_knowledge_bindings(getattr(console, "dify_knowledge_bindings", "") or "")


def _console_settings_from(path: Path) -> dict[str, str]:
    """Read the Dify console keys out of an explicit ``--config`` file.

    Kept here rather than in ``gateway_config`` because only this command reads a
    config file *by path*: the gateway process takes the same values from its own
    environment, and giving ``from_env`` a second source would make "which file am
    I reading" ambiguous everywhere.

    ``AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS`` is here for the same reason the
    console keys are: the pull resolves a declared dataset through it, and
    reading it from the process environment instead would mean the one file the
    operator passed to ``--config`` decides most of the pull but not that part.
    That is #625's defect shape — a fix that reaches one consumer and not the
    other.
    """
    from aiops_diagnostics.gateway.gateway_config import _private_config_values

    # #973 / Option A：``--config`` 是显式点名 —— 不存在的路径是写错了，
    # 按 `Settings.from_config` 同一条规则拒掉，不静默当空表。
    values = _private_config_values(path, explicit=True)
    keys = (
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL",
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY",
        "AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID",
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS",
        "AIOPS_GATEWAY_DIFY_TOOL_BINDINGS",
        "AIOPS_GATEWAY_OPS_BINDINGS_FILE",
        # 部署身份（#886）**也在这里**：workspace 按 (租户, 环境) 解析，而 41 上的
        # `--config` 就是 `/etc/aiops-41/production.env`。只从进程 env 读它会让
        # 「文件里写好了、命令却说没配」重演一次 #625 的形态。
        "AIOPS_GATEWAY_ENVIRONMENT",
    )
    return {name.lower().removeprefix("aiops_gateway_"): (values.get(name) or "") for name in keys}


def _dify_console_and_db(ctx: typer.Context, db: Path | None) -> tuple[Any, Path]:
    """Resolve the Dify console settings and the gateway DB path, or exit.

    Shared by ``pull-dify`` and ``sync-from-dify``: both read the **same three
    console keys from the same file** and both need ``--db`` spelled out. Two
    copies of this is how the console-keys-from-``--config`` fix (#625) would
    silently apply to one command and not the other.
    """
    config_file = ctx.obj.get("config_file") if ctx.obj else None
    console = GatewayServerSettings.from_env()
    if config_file is not None:
        # Merge **per key**, not all-or-nothing: an environment that supplies
        # the console keys but not the bindings (or the reverse) must still get
        # the file's value for whichever one it lacks. Deciding on the single
        # condition "is the console key set?" is how one of the four keys ends
        # up silently read from the wrong place.
        from_file = _console_settings_from(Path(config_file).expanduser().resolve())
        console = dataclasses.replace(
            console,
            # #973：判据与主合并同一 —— 进程 env **有没有这个键**（``KEY=`` 的空串
            # 是值、不输），不是「解析出来的属性空不空」；env 缺席时 --config 那行
            # 才算数（operator 点名这份文件就是要它决定这次拉取）。
            **{
                name: value
                for name, value in from_file.items()
                if value and f"AIOPS_GATEWAY_{name.upper()}" not in os.environ
            },
        )
    # 部署身份与两条命令的**其他**密钥同源（同一个 `--config` 文件），因为它就是
    # `AIOPS_GATEWAY_ENVIRONMENT` —— 41 上那份 `/etc/aiops-41/production.env` 里的一行。
    if not console.environment:
        typer.secho(
            "缺少 AIOPS_GATEWAY_ENVIRONMENT：workspace 按 (租户, 环境) 解析，环境取本机部署身份"
            "（放在服务端配置文件或进程环境里）。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not console.dify_console_base_url or not console.dify_console_api_key:
        typer.secho(
            "缺少 Dify 控制台配置：需要 AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL 与 "
            "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY（放在服务端配置文件或进程环境里）。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    database = db or GatewayServerSettings.from_env().database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：缺省会静默回退 XDG 默认并新建空库。请显式给出 --db <gateway.db>，"
            "或 export AIOPS_GATEWAY_DATABASE_FILE=<gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    gateway_schema.startup(database)
    return console, database


def _workspace_for(workspaces: Any, tenant_id: str, environment: str) -> str:
    """这个租户的 workspace —— 解析失败就**抛**，调用方不许跳过（#886）。

    与 ``admin sync-from-dify`` 的旧行为相反：旧代码把一个固定常量套到 N 个租户上，
    多 workspace 之后那等于"把 A 的正文发进 B 的 workspace"。而跳过也不行 ——
    一次 sync 漏掉半个租户却退 0，是最难发现的一种。

    解析函数的四个码（``UNRESOLVED`` / ``CREATING`` / ``FAILED`` / ``UNKNOWN``）在这里
    全部原样上抛：它们各自对应不同的下一步，合成一句话就把那个区别丢了。
    """
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError

    try:
        return workspaces.resolve(tenant_id, environment)
    except DifyWorkspaceError as exc:
        raise DifyWorkspaceError(
            exc.code,
            f"[租户 {tenant_id}] {exc.message}",
            http_status=exc.http_status,
            retryable=exc.retryable,
        ) from exc


def _deployment_environment_or_exit() -> str:
    """CLI 的部署身份 —— 与 `ensure-workspace` 同一条守卫，缺了就退非零。"""
    environment = GatewayServerSettings.from_env().environment
    if not environment:
        typer.secho(
            "缺少 AIOPS_GATEWAY_ENVIRONMENT：workspace 按 (租户, 环境) 解析，环境取本机部署身份",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return environment


def _settings_with_config_file(ctx: typer.Context) -> GatewayServerSettings:
    """进程 env 优先、`--config` 按**单个键**补齐。

    按 key 合并而不是整体替换：`--config` 里只写了 workspace 相关那几行、而 inner_api
    的键在进程 env 里，这是常态 —— 整体替换会把有的那一半丢掉，只补另一半。

    **判据是"进程 env 有没有这个键"，不是"属性是不是空的"**：后者对带非空默认值的键
    （``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT``）永远为假，于是运维照着 runbook
    写进 ``--config`` 的那一行**静默不生效** —— #625 的形态，这次被评审当场复现。
    """
    settings = GatewayServerSettings.from_env()
    config_file = ctx.obj.get("config_file") if ctx.obj else None
    if config_file is None:
        return settings
    from aiops_diagnostics.gateway.gateway_config import _parse_source_value, _private_config_values

    values = _private_config_values(Path(config_file).expanduser().resolve(), explicit=True)
    for name, attribute in _SETTING_FOR.items():
        # #973：「有没有」按键存在性判（与统一来源规则一致 —— 进程 env 里
        # ``KEY=`` 的空串也是一个**值**，不输）；合并进来的值按字段声明
        # 的 ``kind`` 解析，不写裸字符串进 int/bool 字段。
        if name in os.environ:
            continue
        value = values.get(name)
        if value is None:
            continue
        setattr(settings, attribute, _parse_source_value(SETTING_SOURCES[attribute], value))
    return settings


def _onboarding_settings_or_exit(ctx: typer.Context, *, needs_embedding: bool) -> GatewayServerSettings:
    """Read the settings onboarding needs, or exit naming what is missing (#924).

    Same shape as ``ensure-workspace``'s guards: a missing knob is a **usage**
    error (exit 2), not a failed onboarding — the caller can fix it and rerun
    without any state having changed.
    """
    # 与 `ensure-workspace` / `ensure-ready` 共用同一份合并规则：判据是"进程 env 有没有
    # 这个键"，不是"属性是不是空的"。后者对带非空默认值的键永远为假，运维写进 `--config`
    # 的那一行就**静默不生效**（#625 的形态，评审在 `ensure-ready` 上当场复现过）。
    settings = _settings_with_config_file(ctx)

    required = ["AIOPS_GATEWAY_ENVIRONMENT", "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY"]
    if needs_embedding:
        required += ["AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL", "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY"]
    missing = [name for name in required if not getattr(settings, _SETTING_FOR[name], "")]
    if missing:
        typer.secho(
            "缺少 " + " / ".join(missing) + "：开户要有部署身份、凭据加密密钥与可校验的 embedding 端点",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return settings


def _name_overrides(value: str) -> dict[str, str]:
    """``<库 id>=<名字>,<库 id>=<名字>`` → 字典。"""
    out: dict[str, str] = {}
    for pair in filter(None, (part.strip() for part in value.split(","))):
        key, separator, name = pair.partition("=")
        if not separator or not key.strip() or not name.strip():
            typer.secho(
                f"--name 里有一项格式不对：{pair!r}（应为 <库 id>=<名字>）", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(code=2)
        out[key.strip()] = name.strip()
    return out
