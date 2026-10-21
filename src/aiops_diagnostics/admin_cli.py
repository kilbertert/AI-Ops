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
    knowledge_base_id: Annotated[
        list[str] | None,
        typer.Option(
            "--knowledge-base-id",
            help=(
                "绑给这个 agent 的**我们的**知识库 id（可重复）。Dify 的 dataset id 不是它 —— "
                "DSL 声明了数据集而这里没给，命令会拒，不按字面搬运。"
            ),
        ),
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
        pull_agent_draft,
        resolve_pulled_config,
    )

    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    # 控制台三键与 `admin reconcile` 读**同一个文件**；守卫与 `sync-from-dify` 共用一份，
    # 免得 #625 修的那个「`--config` 里的控制台键读不到」只对其中一条命令生效。
    console, database = _dify_console_and_db(ctx, db)
    # 声明的 dataset 靠这张表翻译成我们的知识库（与检索路由用的是同一张、同一个解析器）。
    knowledge_registry = _dify_knowledge_registry(console)
    tool_registry = _dify_tool_registry(console)

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
            #
            # ⚠️ 走的是**真跑那条同一个函数**。这里曾经自己映射一遍，于是真跑会
            # 保留库中知识库绑定、干跑却报空的 —— 同一份输入两个答案。凡是
            # "干跑与真跑各算一次"的地方都会这样漂，所以只有一处算。
            from aiops_diagnostics.dify_dsl_pull import resolve_pulled_config

            config = resolve_pulled_config(
                manager,
                context,
                source,
                name=name,
                agent_type=agent_type,
                output_contract=output_contract,
                knowledge_base_ids=None if knowledge_base_id is None else tuple(knowledge_base_id),
                knowledge_registry=knowledge_registry,
                tool_registry=tool_registry,
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
            knowledge_base_ids=None if knowledge_base_id is None else tuple(knowledge_base_id),
            knowledge_registry=knowledge_registry,
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


def _dify_knowledge_registry(console: Any) -> Any:
    """The operator's ``knowledge_id → (tenant, our kb ids)`` table, parsed once.

    The **same** parser the inbound retrieval route uses
    (``parse_dify_knowledge_bindings``); the pull reads it in the other
    direction. Sharing the parser is the point — two parsers for one table is
    the "one rule, two implementations" defect this repo keeps re-learning.
    """
    from aiops_diagnostics.gateway_config import parse_dify_knowledge_bindings

    return parse_dify_knowledge_bindings(getattr(console, "dify_knowledge_bindings", "") or "")


def _dify_tool_registry(console: Any) -> Any:
    """Dify 工具身份 → ``ToolName`` 的登记表，与知识库那张同一条解析纪律。"""
    from aiops_diagnostics.gateway_config import parse_dify_tool_bindings

    return parse_dify_tool_bindings(getattr(console, "dify_tool_bindings", "") or "")


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
    from aiops_diagnostics.gateway_config import _private_config_values

    values = _private_config_values(path)
    keys = (
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL",
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY",
        "AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID",
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS",
        "AIOPS_GATEWAY_DIFY_TOOL_BINDINGS",
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
            **{name: value for name, value in from_file.items() if value and not getattr(console, name, "")},
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
    return console, database


@admin_app.command("sync-from-dify")
def sync_from_dify(
    ctx: typer.Context,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="gateway 数据库文件（与 reconcile 同一条守卫）"),
    ] = None,
    kb_url: Annotated[
        str | None,
        typer.Option(
            "--kb-url",
            envvar="AIOPS_GATEWAY_KB_SERVICE_BASE_URL",
            help="kb-service 地址（带 KB 绑定时发布校验需要）",
        ),
    ] = None,
    tenant: Annotated[
        str | None,
        typer.Option("--tenant", help="只同步这个租户（缺省：注册表里全部）"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只报告会做什么，零写入")] = False,
) -> None:
    """把 Dify 上的正文同步成已发布版本 —— **运营改完 Dify 之后的那一步**（无参数）。

    `pull-dify` 要七个参数（含 app UUID），因为它是一条**装配**动作：把某个 app 落到某个
    租户的某个 agent 上。而运营改完正文之后要的是一件**例行**动作：把「现在生效的那一版」
    更新成 Dify 里现在这一份。两者需要的信息量不同，所以是两条命令，不是一条带默认值的。

    七个参数里除了 `app_id` **全部从库里推出来**：

    * `app_id` / `agent_name` / `tenant_id` —— 运行时注册表那三行（#584）；
    * `agent_type` / `output_contract` / `description` / `knowledge_base_ids` —— 库里
      那个 agent 现在是什么。

    因此这条命令**不改路由、不改类型、不改知识库**：它只把正文（与模型）更新成 Dify 的
    现值。要改别的，那是清单 + `reconcile` 的事，需要人显式决定。

    **幂等靠比较，不靠信任**：Dify **没有草稿区**（实测：保存即导出，未点 Publish 的
    正文立刻可被导出），所以 Dify 说不出"这一版还没定稿"。唯一诚实的门是**变化**——
    拉回的正文与**已发布**版本逐字相同就什么都不写。没有这一层，每次跑都会 fork 出一个
    内容相同的新版本，线上正在服务的那一版会在脚下被换掉。

    **为什么不是自动定时跑**：正因为 Dify 没有草稿区，定时跑会**把运营打字打到一半的
    正文推上线**。所以这是一条**显式动作**，由知道"我改完了"的人触发。
    """
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore, allowed_models_from_settings
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        sync_binding_from_dify,
    )

    console, database = _dify_console_and_db(ctx, db)
    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    knowledge_registry = _dify_knowledge_registry(console)
    tool_registry = _dify_tool_registry(console)
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
    bindings = [
        binding
        for binding in DifyAppRegistry(database).all()
        if tenant is None or binding.tenant_id == tenant
    ]
    if not bindings:
        typer.echo(
            json.dumps(
                {"dry_run": dry_run, "outcomes": [], "note": "注册表里没有可同步的绑定"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    outcomes: list[dict[str, Any]] = []
    worst = 0
    for binding in bindings:
        source = DifyDslSource(
            base_url=console.dify_console_base_url,
            app_id=binding.app_id,
            api_key=console.dify_console_api_key,
            workspace_id=console.dify_console_workspace_id,
        )
        try:
            outcome = sync_binding_from_dify(
                manager,
                admin_context(binding.tenant_id),
                binding,
                source,
                dry_run=dry_run,
                knowledge_registry=knowledge_registry,
                tool_registry=tool_registry,
            )
            outcomes.append(dataclasses.asdict(outcome))
            if outcome.action == "failed":
                worst = max(worst, 1)
        except DifyDslError as exc:
            outcomes.append(
                {
                    "tenant_id": binding.tenant_id,
                    "business_entry": binding.business_entry,
                    "app_id": binding.app_id,
                    "agent_name": binding.agent_name,
                    "action": "failed",
                    "version_no": None,
                    "note": f"[{exc.code}] {exc}",
                }
            )
            worst = max(worst, 1)
        except Exception as exc:  # noqa: BLE001 - 逐个绑定转述，不让一个坏绑定吞掉其余
            outcomes.append(
                {
                    "tenant_id": binding.tenant_id,
                    "business_entry": binding.business_entry,
                    "app_id": binding.app_id,
                    "agent_name": binding.agent_name,
                    "action": "failed",
                    "version_no": None,
                    "note": f"{type(exc).__name__}: {exc}",
                }
            )
            worst = max(worst, 1)
    typer.echo(json.dumps({"dry_run": dry_run, "outcomes": outcomes}, ensure_ascii=False, indent=2))
    if worst:
        raise typer.Exit(code=1)


@admin_app.command("ensure-workspace")
def ensure_workspace_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    db: Annotated[
        Path | None,
        typer.Option("--db", help="gateway 数据库文件（缺省会回退 XDG 默认并新建空库）"),
    ] = None,
) -> None:
    """把一个租户的 Dify workspace **真的建出来**（#880）。

    **环境取本机部署身份，不是参数** —— 与 `resolve_workspace` 同一条规则：允许跨环境
    建同一个 `tenant_id` 的 workspace，等于把隔离交给调用方选。

    幂等：已 `created`/`ready` 的租户一条写都不发；`creating` 且租约未过期的报"有人在建"；
    `failed` 重走一次；`unknown` 或租约过期的 `creating` **先按名字对账**，命中 1 个就补
    UUID（不重建）、命中 0 个才重建、命中 >1 个拒绝并要人工。
    """
    from aiops_diagnostics.dify_ensure_workspace import (
        PLATFORM_OWNER_EMAIL,
        EnsureError,
        ensure_workspace,
        render,
    )
    from aiops_diagnostics.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    settings = GatewayServerSettings.from_env()
    config_file = ctx.obj.get("config_file") if ctx.obj else None
    if config_file is not None:
        from aiops_diagnostics.gateway_config import _private_config_values

        values = _private_config_values(Path(config_file).expanduser().resolve())
        for name in (
            "AIOPS_GATEWAY_ENVIRONMENT",
            "AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL",
            "AIOPS_GATEWAY_DIFY_INNER_API_KEY",
            "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL",
            "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY",
        ):
            if not getattr(settings, _SETTING_FOR[name], ""):
                setattr(settings, _SETTING_FOR[name], values.get(name) or "")

    if not settings.environment:
        typer.secho(
            "缺少 AIOPS_GATEWAY_ENVIRONMENT：环境取本机部署身份，不是参数",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    inner = DifyInnerApi(
        InnerApiSettings(
            base_url=settings.dify_inner_api_base_url,
            api_key=settings.dify_inner_api_key,
        )
    )
    if not inner.settings.configured:
        typer.secho(
            "缺少 AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL / _KEY：建 workspace 的唯一入口是 inner_api",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not (settings.dify_console_base_url and settings.dify_console_api_key):
        typer.secho(
            "缺少 AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL / _API_KEY：对账（all-workspaces）要用它",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    database = db or settings.database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：缺省会静默回退 XDG 默认并新建空库。请显式给出 --db <gateway.db>，"
            "或 export AIOPS_GATEWAY_DATABASE_FILE=<gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        outcome = ensure_workspace(
            tenant_id=tenant,
            environment=settings.environment,
            workspaces=DifyWorkspaces(database),
            inner=inner,
            console_base_url=settings.dify_console_base_url,
            console_api_key=settings.dify_console_api_key,
            log=lambda line: typer.secho(line, err=True),
        )
    except EnsureError as exc:
        typer.secho(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(render(outcome))
    typer.echo(f"platform owner: {PLATFORM_OWNER_EMAIL}", err=True)


#: CLI 参数名 → `GatewayServerSettings` 字段。放在函数外，字典只写一次 —— 上面那个循环
#: 复制五份 `setattr` 字符串是"同一规则两份实现"的经典起点。
_SETTING_FOR = {
    "AIOPS_GATEWAY_ENVIRONMENT": "environment",
    "AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL": "dify_inner_api_base_url",
    "AIOPS_GATEWAY_DIFY_INNER_API_KEY": "dify_inner_api_key",
    "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": "dify_console_base_url",
    "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": "dify_console_api_key",
}
