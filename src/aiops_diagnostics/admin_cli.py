"""``aiops admin`` — declarative gateway management commands.

Currently one command: ``admin reconcile`` converges the gateway agent store
toward an ``ops/environments/<env>.toml`` manifest through AgentManager (the
production code path). See :mod:`aiops_diagnostics.agent_manifest`.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from aiops_diagnostics.config import Settings, selected_config_file
from aiops_diagnostics.gateway_config import GatewayServerSettings

admin_app = typer.Typer(name="admin", help="AI-Ops 网关管理操作（环境清单收敛）", no_args_is_help=True)


def _platform_migration_context() -> Any:
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="platform-shortcut-migration", tenant_id="platform")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="platform",
        data_scope=DataScope(type="all"),
        roles=frozenset({"ROLE_PLATFORM_ADMIN"}),
        permissions=frozenset({"aiops:shortcuts:manage"}),
    )


@admin_app.command("migrate-shortcuts")
def migrate_shortcuts(
    db: Annotated[Path, typer.Option("--db", exists=True, dir_okay=False, help="gateway 数据库文件")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只输出计划，零写入")] = False,
) -> None:
    """将租户复制的快捷动作幂等收敛为平台默认 + 租户覆盖。"""
    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore
    from aiops_diagnostics.shortcut_migration import migrate_shortcuts as run_migration

    reports = run_migration(
        ShortcutManager(ShortcutStore(db)), _platform_migration_context(), dry_run=dry_run
    )
    typer.echo(
        json.dumps(
            {"dry_run": dry_run, "reports": [dataclasses.asdict(report) for report in reports]},
            ensure_ascii=False,
            indent=2,
        )
    )


def _reconcile_settings(config_file: Path | None) -> Settings:
    path = config_file if config_file is not None else selected_config_file()
    return Settings.from_config(path)


@admin_app.command("reconcile")
def reconcile(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="环境清单 TOML")],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="gateway 数据库文件（必填：--config 不喂数据库路径，缺省会回退 XDG 默认新建空库）",
        ),
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
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore, allowed_models_from_settings
    from aiops_diagnostics.agent_manifest import ManifestError, load_manifest, reconcile_dify_registry
    from aiops_diagnostics.agent_manifest import reconcile as reconcile_manifest
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry

    try:
        environment = load_manifest(manifest)
    except ManifestError as exc:
        typer.secho(f"清单无效: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    # XDG 陷阱守卫（41 实机事故，见 docs/validation.md M55）：完全不显式给库时，
    # 缺省会静默回退 GatewayServerSettings.from_env() 的 XDG 路径并新建空库，
    # 收敛全 created。显式 --db 或显式 AIOPS_GATEWAY_DATABASE_FILE 才执行。
    database = db or GatewayServerSettings.from_env().database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：--config 不会解析数据库路径，缺省会静默回退 XDG 默认并新建空库。"
            "请显式给出 --db <gateway.db>，或 export AIOPS_GATEWAY_DATABASE_FILE=<gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    knowledge_resolver = None
    if kb_url:
        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(kb_url, tenant_id="aiops", timeout=10)
        )
    manager = AgentManager(
        AgentStore(database),
        knowledge_resolver=knowledge_resolver,
        allowed_models=allowed_models_from_settings(settings),
    )

    try:
        reports: list[Any] = reconcile_manifest(manager, environment, prune=prune, dry_run=dry_run)
        # The runtime registry (#584) converges in the same run: it is the same
        # declared state, and splitting it into a second command would let the
        # two drift out of step between invocations.
        registry_reports = reconcile_dify_registry(
            DifyAppRegistry(database), environment, prune=prune, dry_run=dry_run
        )
    except ManifestError as exc:
        typer.secho(f"收敛中止（库未变更）: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        json.dumps(
            {
                "dry_run": dry_run,
                "reports": [dataclasses.asdict(report) for report in reports],
                "dify_registry": [dataclasses.asdict(report) for report in registry_reports],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@admin_app.command("pull-dify")
def pull_dify(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Option("--app-id", help="Dify 控制台里的 app id")],
    tenant: Annotated[str, typer.Option("--tenant", help="发布到哪个租户")],
    name: Annotated[str, typer.Option("--name", help="我方 agent 名（同名即原地收敛）")],
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="gateway 数据库文件（与 reconcile 同一条守卫：缺省会静默回退 XDG 建空库）",
        ),
    ] = None,
    description: Annotated[str, typer.Option("--description", help="agent 说明")] = "",
    agent_type: Annotated[
        str, typer.Option("--agent-type", help="customer | operations（Dify 没有这个概念）")
    ] = "customer",
    output_contract: Annotated[
        str | None, typer.Option("--output-contract", help="缺省按 agent_type 推导")
    ] = None,
    kb_url: Annotated[
        str | None,
        typer.Option(
            "--kb-url",
            envvar="AIOPS_GATEWAY_KB_SERVICE_BASE_URL",
            help="kb-service 地址（发布前的 KB 活性校验需要）",
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只拉取与映射，不落草稿、不发布")] = False,
) -> None:
    """把运营在 Dify 里改好的配置拉回来，经**我方发布门**冻结成一个已发布版本（#625）。

    这是阶段 1 那条链的第二跳（第一跳是运营在 Dify 控制台里改，第三跳是运行时服务提问）。
    三处形状是刻意选的，不是默认：

    * **拉取**用控制台导出端点（`GET /console/api/apps/<id>/export`），凭据来自**服务端配置**
      `AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY`（不是命令行参数：那会进 shell 历史与进程表）。
    * **发布**走 `AgentManager.publish` —— 与 `/v1/agents` 和 `admin reconcile` **同一条**
      生产路径（角色校验、模型白名单、发布前 KB 活性校验）。不绕过去写库。
    * **不写登记表**：这次拉取把哪一个 app 落到哪个 agent 是**这一次的参数**；"哪个 app 服务
      哪个租户/入口"是运行时注册表的事（`[[dify_apps]]` + `admin reconcile`，见 #584）。
      两件事混在一起会让一次拉取顺手改掉线上路由。
    """
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore, allowed_models_from_settings
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        map_dsl_to_config,
        pull_agent_draft,
    )

    config_file = ctx.obj.get("config_file") if ctx.obj else None
    settings = _reconcile_settings(config_file)
    # 控制台三键与 `admin reconcile` 读**同一个文件**。
    #
    # ⚠️ 这里踩过一次：`GatewayServerSettings.from_env()` 只会去看
    # `AIOPS_GATEWAY_SERVER_CONFIG_FILE` / 默认路径，**不看 `--config`**。41 上
    # `--config /etc/aiops-41/production.env` 是既有形状（runbook §2/§3 全这么写），
    # 而那个变量在 systemd 里指向 `gateway.env` —— 于是配置明明在 `--config` 指定的
    # 文件里，命令却报"缺少 Dify 控制台配置"。既有三个命令没有暴露这个问题，是因为
    # 它们的控制台键只在网关进程里用（那里 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 是对的）。
    console = GatewayServerSettings.from_env()
    if config_file is not None and not console.dify_console_api_key:
        console = dataclasses.replace(
            console, **_console_settings_from(Path(config_file).expanduser().resolve())
        )
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

    knowledge_resolver = None
    if kb_url:
        from aiops_diagnostics.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(kb_url, tenant_id="aiops", timeout=10)
        )
    manager = AgentManager(
        AgentStore(database),
        knowledge_resolver=knowledge_resolver,
        allowed_models=allowed_models_from_settings(settings),
    )
    context = admin_context(tenant)
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=app_id,
        api_key=console.dify_console_api_key,
        workspace_id=console.dify_console_workspace_id,
    )
    try:
        if dry_run:
            # 拉取、解析、映射全部完成于第一次写入之前（#580 已保证），所以 dry-run
            # 只要走到映射这一步就能回答"这次会得到什么"，且**零写入**。
            from aiops_diagnostics.dify_dsl_pull import DifyDslClient

            config = map_dsl_to_config(
                DifyDslClient(source).export_dsl(),
                agent_type=agent_type,
                output_contract=output_contract,
            )
            typer.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "app_id": app_id,
                        "tenant_id": tenant,
                        "name": name,
                        "config": config.to_dict(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        agent = pull_agent_draft(
            manager,
            context,
            source,
            name=name,
            description=description,
            agent_type=agent_type,
            output_contract=output_contract,
        )
        draft_revision = agent.revision
        if agent.status != "draft" and agent.published_version is not None:
            # converge 会把已发布的分支成草稿，这里再取一次拿新的 revision。
            agent = manager.get(context, agent.agent_id)
            draft_revision = agent.revision
        version = manager.publish(context, agent.agent_id, expected_revision=draft_revision)
    except DifyDslError as exc:
        typer.secho(f"拉取失败（库未变更）: [{exc.code}] {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - 逐类转述，不吞
        typer.secho(f"发布失败（库未变更）: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "app_id": app_id,
                "tenant_id": tenant,
                "agent_id": agent.agent_id,
                "agent_name": agent.name,
                "version_no": version.version_no,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _console_settings_from(path: Path) -> dict[str, str]:
    """Read the three console keys out of an explicit ``--config`` file.

    Kept here rather than in ``gateway_config`` because only this command reads a
    config file *by path*: the gateway process takes the same values from its own
    environment, and giving ``from_env`` a second source would make "which file am
    I reading" ambiguous everywhere.
    """
    from aiops_diagnostics.gateway_config import _private_config_values

    values = _private_config_values(path)
    keys = (
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL",
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY",
        "AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID",
    )
    return {name.lower().removeprefix("aiops_gateway_"): (values.get(name) or "") for name in keys}
