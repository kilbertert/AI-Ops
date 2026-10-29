"""独立运维入口：环境收敛、开户、知识同步和发布管理。"""

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


def _platform_migration_context() -> Any:
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

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
    from aiops_diagnostics.publication.shortcut_lifecycle import ShortcutManager, ShortcutStore
    from aiops_diagnostics.publication.shortcut_migration import migrate_shortcuts as run_migration

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
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient
    from aiops_diagnostics.publication.agent_lifecycle import (
        AgentManager,
        AgentStore,
        allowed_models_from_settings,
    )
    from aiops_diagnostics.publication.agent_manifest import (
        ManifestError,
        load_manifest,
        reconcile_dify_registry,
    )
    from aiops_diagnostics.publication.agent_manifest import reconcile as reconcile_manifest

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
    from aiops_diagnostics.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        pull_agent_draft,
        resolve_pulled_config,
    )
    from aiops_diagnostics.publication.agent_lifecycle import (
        AgentManager,
        AgentStore,
        allowed_models_from_settings,
    )
    from aiops_diagnostics.publication.agent_manifest import admin_context

    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    # 控制台三键与 `admin reconcile` 读**同一个文件**；守卫与 `sync-from-dify` 共用一份，
    # 免得 #625 修的那个「`--config` 里的控制台键读不到」只对其中一条命令生效。
    console, database = _dify_console_and_db(ctx, db)
    # 声明的 dataset 靠这张表翻译成我们的知识库（与检索路由用的是同一张、同一个解析器）。
    knowledge_registry = _dify_knowledge_registry(console)

    knowledge_resolver = None
    if kb_url:
        from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(kb_url, tenant_id="aiops", timeout=10)
        )
    manager = AgentManager(
        AgentStore(database),
        knowledge_resolver=knowledge_resolver,
        allowed_models=allowed_models_from_settings(settings),
    )
    context = admin_context(tenant)
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    workspaces = DifyWorkspaces(database)
    resolved_workspace = _workspace_for(workspaces, tenant, console.environment)
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=app_id,
        api_key=console.dify_console_api_key,
        workspace_id=resolved_workspace,
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
            **{name: value for name, value in from_file.items() if value and not getattr(console, name, "")},
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
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        sync_binding_from_dify,
    )
    from aiops_diagnostics.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
    from aiops_diagnostics.publication.agent_lifecycle import (
        AgentManager,
        AgentStore,
        allowed_models_from_settings,
    )
    from aiops_diagnostics.publication.agent_manifest import admin_context

    console, database = _dify_console_and_db(ctx, db)
    settings = _reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    knowledge_registry = _dify_knowledge_registry(console)
    knowledge_resolver = None
    if kb_url:
        from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

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
    workspaces = DifyWorkspaces(database)
    environment = console.environment
    # **先全部解析，再开始同步**（评审要点，也是本票的核心判据）：一个租户解析不出来，
    # 整条命令在做任何一次写之前就拒绝。逐个"边解析边同步"会让前半段已经写进生产，
    # 而退出码仍是非零 —— 那时"部分成功"已经发生了，回滚无从谈起。
    resolved: dict[str, str] = {}
    unresolved: list[dict[str, str]] = []
    for binding in bindings:
        try:
            resolved[binding.tenant_id] = _workspace_for(workspaces, binding.tenant_id, environment)
        except DifyWorkspaceError as exc:
            unresolved.append({"tenant_id": binding.tenant_id, "code": exc.code, "message": exc.message})
    if unresolved:
        typer.echo(
            json.dumps(
                {
                    "dry_run": dry_run,
                    "refused": "有租户解析不到 workspace，整条命令不做任何写",
                    "environment": environment,
                    "unresolved": unresolved,
                    "outcomes": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1)

    for binding in bindings:
        source = DifyDslSource(
            base_url=console.dify_console_base_url,
            app_id=binding.app_id,
            api_key=console.dify_console_api_key,
            workspace_id=resolved[binding.tenant_id],
        )
        try:
            outcome = sync_binding_from_dify(
                manager,
                admin_context(binding.tenant_id),
                binding,
                source,
                dry_run=dry_run,
                knowledge_registry=knowledge_registry,
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


def _workspace_for(workspaces: Any, tenant_id: str, environment: str) -> str:
    """这个租户的 workspace —— 解析失败就**抛**，调用方不许跳过（#886）。

    与 ``admin sync-from-dify`` 的旧行为相反：旧代码把一个固定常量套到 N 个租户上，
    多 workspace 之后那等于"把 A 的正文发进 B 的 workspace"。而跳过也不行 ——
    一次 sync 漏掉半个租户却退 0，是最难发现的一种。

    解析函数的四个码（``UNRESOLVED`` / ``CREATING`` / ``FAILED`` / ``UNKNOWN``）在这里
    全部原样上抛：它们各自对应不同的下一步，合成一句话就把那个区别丢了。
    """
    from aiops_diagnostics.dify_workspaces import DifyWorkspaceError

    try:
        return workspaces.resolve(tenant_id, environment)
    except DifyWorkspaceError as exc:
        raise DifyWorkspaceError(
            exc.code,
            f"[租户 {tenant_id}] {exc.message}",
            http_status=exc.http_status,
            retryable=exc.retryable,
        ) from exc


def _workspace_for(workspaces: Any, tenant_id: str, environment: str) -> str:
    """这个租户的 workspace —— 解析失败就**抛**，调用方不许跳过（#886）。

    与 ``admin sync-from-dify`` 的旧行为相反：旧代码把一个固定常量套到 N 个租户上，
    多 workspace 之后那等于"把 A 的正文发进 B 的 workspace"。而跳过也不行 ——
    一次 sync 漏掉半个租户却退 0，是最难发现的一种。

    解析函数的四个码（``UNRESOLVED`` / ``CREATING`` / ``FAILED`` / ``UNKNOWN``）在这里
    全部原样上抛：它们各自对应不同的下一步，合成一句话就把那个区别丢了。
    """
    from aiops_diagnostics.dify_workspaces import DifyWorkspaceError

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
    from aiops_diagnostics.gateway_config import _private_config_values

    values = _private_config_values(Path(config_file).expanduser().resolve())
    for name, attribute in _SETTING_FOR.items():
        value = values.get(name) or ""
        if value and not os.environ.get(name):
            setattr(settings, attribute, value)
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


@admin_app.command("check-knowledge-triple")
def check_knowledge_triple_command(
    ctx: typer.Context,
    db: Annotated[Path | None, typer.Option("--db", help="gateway 数据库文件")] = None,
    workspace_names: Annotated[
        str,
        typer.Option(
            "--workspace-names",
            help="Dify 的 workspace 列表（`id=name,id=name`）；不给则不判断 workspace 是否真的在 Dify 里",
        ),
    ] = "",
) -> None:
    """一个租户"真的能用知识库"要的三样东西齐不齐（#927）。

    三项**分开报**：缺 workspace / 缺 RAGFlow 租户 / 模型没校验过。三种半成品的修法不同，
    压成一个"不完整"会让人回头再查三张表。

    退出码：0 全部完整 / 1 有不完整的租户 / 2 缺配置。
    """
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.knowledge_triple import check, render

    settings = GatewayServerSettings.from_env()
    database = (db or settings.database_file).expanduser()
    # **Refuse a database that is not there.** `GatewayStore` creates one on open,
    # so a typo would hand back an empty store whose "no tenants" reads as a clean
    # bill of health — an inspection of the wrong file presented as a pass.
    if not database.is_file():
        typer.secho(
            f"数据库不存在：{database}。核对要读真实库，不能新建一个空的（那样每个租户都是 0 个）。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    names: dict[str, str] = {}
    for pair in filter(None, (part.strip() for part in workspace_names.split(","))):
        key, _, value = pair.partition("=")
        if not key.strip():
            typer.secho(f"--workspace-names 里有一项没有 id：{pair!r}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        names[key.strip()] = value.strip()

    results = check(
        store=GatewayStore(database), environment=settings.environment, dify_workspace_names=names
    )
    typer.echo(render(results))
    raise typer.Exit(code=0 if all(item.ok for item in results) else 1)


@admin_app.command("plan-knowledge-migration")
def plan_knowledge_migration_command(
    ctx: typer.Context,
    workspace: Annotated[str, typer.Option("--workspace", help="目标租户 workspace 的 Dify tenant id")],
    datasets: Annotated[
        str, typer.Option("--datasets", help="JSON：数据集行（id/name/tenant_id/external_knowledge_api_id）")
    ],
    apis: Annotated[str, typer.Option("--apis", help="JSON：外部知识库模板行（id/name/tenant_id）")],
) -> None:
    """只算不改：把外部知识库配置搬进租户 workspace 会发生什么（#927）。

    输入是两棵快照（由调用方从 Dify 的 Postgres 导出），所以这条命令不连数据库、
    不写任何东西 —— 三个已知坑都在"决定"里，把它们做成一个可以反复推演的纯函数。
    """
    import json as _json

    from aiops_diagnostics.dify_knowledge_migration import plan

    try:
        dataset_rows = _json.loads(datasets)
        api_rows = _json.loads(apis)
    except _json.JSONDecodeError as exc:
        typer.secho(f"输入不是合法 JSON：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    result = plan(dataset_rows, api_rows, workspace_id=workspace)
    typer.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    typer.secho("以上是计划，没有写任何东西。搬完之后用同一份输入跑 verify。", err=True)


@admin_app.command("onboard-tenant")
def onboard_tenant_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    db: Annotated[
        Path | None,
        typer.Option("--db", help="gateway 数据库文件（缺省会回退 XDG 默认并新建空库）"),
    ] = None,
    skip_workspace: Annotated[
        bool,
        typer.Option("--skip-workspace", help="只建 RAGFlow 那一半（workspace 已由 ensure-workspace 建好）"),
    ] = False,
) -> None:
    """把一个租户的 **1:1:1 三个对象**建齐（#924 / PRD #923）。

    workspace → RAGFlow 租户 → 模型实例，任一步失败整条非零退出并指出是哪一步。幂等：
    已校验过的租户一条写都不发；**半成品会被补齐**（有行但没校验过 = 上次模型校验失败）。

    它**不建**任何知识库、不改任何智能体、不动 Dify 的 app。
    """
    from aiops_diagnostics.dify_ensure_workspace import (
        PLATFORM_OWNER_EMAIL,
        EnsureError,
    )
    from aiops_diagnostics.dify_ensure_workspace import (
        ensure_workspace as _ensure_workspace,
    )
    from aiops_diagnostics.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import KbAdminClient, OnboardError, onboard_tenant

    settings = _onboarding_settings_or_exit(ctx, needs_embedding=True)
    database = db or settings.database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：缺省会静默回退 XDG 默认并新建空库。请显式给出 --db <gateway.db>，"
            "或 export AIOPS_GATEWAY_DATABASE_FILE=<gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not settings.kb_service_base_url:
        typer.secho(
            "缺少 AIOPS_GATEWAY_KB_SERVICE_BASE_URL：开户要打 kb-service 的 /kb/tenants",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    workspace_step = None
    if not skip_workspace:
        inner = DifyInnerApi(
            InnerApiSettings(
                base_url=settings.dify_inner_api_base_url,
                api_key=settings.dify_inner_api_key,
            )
        )
        if not inner.settings.configured:
            typer.secho(
                "缺少 AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL / _KEY："
                "建 workspace 的唯一入口是 inner_api（或用 --skip-workspace 只建 RAGFlow 那一半）",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        workspaces = DifyWorkspaces(database)

        def workspace_step() -> object:
            return _ensure_workspace(
                tenant_id=tenant,
                environment=settings.environment,
                workspaces=workspaces,
                inner=inner,
                console_base_url=settings.dify_console_base_url,
                console_api_key=settings.dify_console_api_key,
                log=lambda line: typer.secho(line, err=True),
            )

    try:
        outcome = onboard_tenant(
            tenant_id=tenant,
            environment=settings.environment,
            store=GatewayStore(database),
            kb=KbAdminClient(settings.kb_service_base_url),
            credential_key=settings.ragflow_credential_key,
            embedding_base_url=settings.ragflow_embedding_base_url,
            embedding_api_key=settings.ragflow_embedding_api_key,
            embedding_model=settings.ragflow_embedding_model,
            tenant_domain=settings.ragflow_tenant_domain,
            ensure_workspace=workspace_step,
            log=lambda line: typer.secho(line, err=True),
        )
    except (OnboardError, EnsureError) as exc:
        payload = {"error": exc.code, "message": exc.message}
        if isinstance(exc, OnboardError):
            payload["step"] = exc.step
        typer.secho(json.dumps(payload, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(outcome.as_dict(), ensure_ascii=False))
    typer.secho(f"platform owner: {PLATFORM_OWNER_EMAIL}", err=True)


@admin_app.command("adopt-ragflow-tenant")
def adopt_ragflow_tenant_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    db: Annotated[Path | None, typer.Option("--db", help="gateway 数据库文件")] = None,
) -> None:
    """接管一个**上游早已有账号、本地却没有凭据**的租户（#923）。

    口令**从 stdin 读一行**，不接受命令行参数 —— ``ps``、shell 历史与作业日志都会留下 argv。

        printf '%s' "$PASSWORD" | aiops-admin adopt-ragflow-tenant --tenant 1942... --db ...

    这条命令**不改上游**：它验证「这一把现在能不能登录」，能才登记。改密码那一步在带外
    完成（历史账号由 kb-service 懒注册生成随机密码且不留，只能这样接管）。
    """
    import sys

    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import (
        KbAdminClient,
        OnboardError,
        adopt_existing_account,
    )

    settings = _onboarding_settings_or_exit(ctx, needs_embedding=False)
    database = db or settings.database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：缺省会静默回退 XDG 默认并新建空库。请显式给出 --db <gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not settings.kb_service_base_url:
        typer.secho("缺少 AIOPS_GATEWAY_KB_SERVICE_BASE_URL", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    password = sys.stdin.readline().rstrip("\n")
    try:
        outcome = adopt_existing_account(
            tenant_id=tenant,
            environment=settings.environment,
            store=GatewayStore(database),
            kb=KbAdminClient(settings.kb_service_base_url),
            credential_key=settings.ragflow_credential_key,
            password=password,
            log=lambda line: typer.secho(line, err=True),
        )
    except OnboardError as exc:
        typer.secho(
            json.dumps({"error": exc.code, "message": exc.message, "step": exc.step}, ensure_ascii=False),
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(outcome, ensure_ascii=False))


@admin_app.command("rotate-ragflow-credential")
def rotate_ragflow_credential_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    db: Annotated[Path | None, typer.Option("--db", help="gateway 数据库文件")] = None,
) -> None:
    """换掉一个租户的 RAGFlow 密码（#924）。旧密码立即失效。

    独立命令而不是开户的一个开关：开户必须保持"再跑一次什么都不变"，轮换不能是它的副作用。
    """
    from aiops_diagnostics.dify_inner_api import InnerApiError  # noqa: F401 - 保持帮助一致的导入形状
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import KbAdminClient, OnboardError, rotate_credential

    settings = _onboarding_settings_or_exit(ctx, needs_embedding=False)
    database = db or settings.database_file
    try:
        result = rotate_credential(
            tenant_id=tenant,
            environment=settings.environment,
            store=GatewayStore(database),
            kb=KbAdminClient(settings.kb_service_base_url),
            credential_key=settings.ragflow_credential_key,
            log=lambda line: typer.secho(line, err=True),
        )
    except OnboardError as exc:
        typer.secho(
            json.dumps({"error": exc.code, "message": exc.message, "step": exc.step}, ensure_ascii=False),
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


@admin_app.command("ensure-workspace")
def ensure_workspace_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    company: Annotated[
        str,
        typer.Option("--company", help="公司名（只影响建的那一刻取的名字，写进表后不再重算）"),
    ] = "",
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

    settings = _settings_with_config_file(ctx)

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
            company=company,
            log=lambda line: typer.secho(line, err=True),
        )
    except EnsureError as exc:
        typer.secho(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(render(outcome))
    typer.echo(f"platform owner: {PLATFORM_OWNER_EMAIL}", err=True)


@admin_app.command("grant-tenant-access")
def grant_tenant_access_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id），或 platform")],
    account_id: Annotated[str, typer.Option("--account-id", help="该人**在 Dify 里**的账号 id")],
    email: Annotated[str, typer.Option("--email", help="该账号在 Dify 里的 email（必须逐字一致）")],
    role: Annotated[str, typer.Option("--role", help="editor（默认）/ admin / normal")] = "editor",
) -> None:
    """把一个**运维账号**加进某个租户的 workspace（Dify 控制台里切租户的前提）。

    为什么需要它：Dify 的 workspace 切换器只在一个条件下隐藏 —— 这个账号的 workspace
    列表为空。成员关系一到，切换器就在导航栏的 workspace 卡片里出现，点一下调
    `/workspaces/switch`。**不需要改 Dify、不需要重建 web 镜像。**

    **不碰 `current`**：切哪个是人在 Dify 里点的。替人改会让别的标签页看到状态跳动。

    **按需跑，不要批量**：25 个 `ROLE_ADMIN` 里 18 个半年以上没登录过，全量 join 等于
    给休眠账号开每一个租户的 workspace 访问权。
    """
    from aiops_diagnostics.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify_tenant_access import GrantError, grant_tenant_access
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    settings = _settings_with_config_file(ctx)
    if not settings.environment:
        typer.secho("缺少 AIOPS_GATEWAY_ENVIRONMENT：环境取本机部署身份", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    inner = DifyInnerApi(
        InnerApiSettings(base_url=settings.dify_inner_api_base_url, api_key=settings.dify_inner_api_key)
    )
    if not inner.settings.configured:
        typer.secho(
            "缺少 AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL / _KEY：join 的唯一入口是 inner_api",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not settings.database_file:
        typer.secho("缺少数据库路径：需要它查 (租户, 环境) → workspace", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        outcome = grant_tenant_access(
            tenant_id=tenant,
            environment=settings.environment,
            workspaces=DifyWorkspaces(settings.database_file),
            inner=inner,
            account_id=account_id,
            email=email,
            role=role,
        )
    except GrantError as exc:
        typer.secho(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))


@admin_app.command("ensure-ready")
def ensure_ready_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id），或 platform")],
    db: Annotated[
        Path | None,
        typer.Option("--db", help="gateway 数据库文件（缺省会回退 XDG 默认并新建空库）"),
    ] = None,
    template: Annotated[
        Path | None,
        typer.Option("--template", help="模板 DSL 文件（缺省：随包发布的那份）", exists=True, dir_okay=False),
    ] = None,
    app_name: Annotated[
        str | None, typer.Option("--app-name", help="模板导进去之后那个 app 的名字（幂等判据）")
    ] = None,
) -> None:
    """把 `ensure-workspace` 建出来的空壳推到 **ready**（#881）。

    三步：装模型插件 → 写这个 workspace 自己的 provider 凭据 → 导模板 DSL。前两步失败
    就停；第三步失败**只记状态**（workspace 与凭据都还在，人进得去），重跑补齐。

    模型凭据来自 ``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY`` —— **代码里没有默认值**，
    缺了就退非零并说去哪填。Dify 会拿它做真校验，假 key 回 ``DIFY_CREDENTIAL_INVALID``。
    """
    from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify_ensure_workspace import EnsureError
    from aiops_diagnostics.dify_workspace_ready import (
        DEFAULT_APP_NAME,
        ReadySettings,
        bundled_template,
        ensure_ready,
    )
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    settings = _settings_with_config_file(ctx)
    if not settings.environment:
        typer.secho(
            "缺少 AIOPS_GATEWAY_ENVIRONMENT：环境取本机部署身份，不是参数",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    ready_settings = ReadySettings(
        console_base_url=settings.dify_console_base_url,
        console_api_key=settings.dify_console_api_key,
        model_api_key=settings.dify_workspace_model_api_key,
        model_endpoint=settings.dify_workspace_model_endpoint,
    )
    gaps = ready_settings.missing()
    if gaps:
        typer.secho(
            "缺少：" + "、".join(gaps) + "。放在服务端配置文件或进程环境里；"
            "AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY 是写进每个 workspace 的模型凭据。",
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
        outcome = ensure_ready(
            tenant_id=tenant,
            environment=settings.environment,
            workspaces=DifyWorkspaces(database),
            console=DifyConsoleAdmin(
                ConsoleApiSettings(
                    base_url=ready_settings.console_base_url,
                    api_key=ready_settings.console_api_key,
                )
            ),
            settings=ready_settings,
            template_yaml=(
                template.read_text(encoding="utf-8") if template is not None else bundled_template()
            ),
            app_name=app_name or DEFAULT_APP_NAME,
            log=lambda line: typer.secho(line, err=True),
        )
    except EnsureError as exc:
        typer.secho(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))


@admin_app.command("ensure-node-provider")
def ensure_node_provider_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Option("--app-id")],
    gateway_url: Annotated[str, typer.Option("--gateway-url")],
    db: Annotated[Path, typer.Option("--db", exists=True, dir_okay=False)],
) -> None:
    """为已登记管家节点应用安装真实CPMS工具定义；不写业务凭据。"""
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.execution.node_publication import target_for
    from aiops_diagnostics.ops_provider import ensure_node_provider

    settings, database = _dify_console_and_db(ctx, db)
    try:
        binding = next(b for b in DifyAppRegistry(database).all() if b.app_id == app_id)
        target = target_for(settings, binding)
        if target is None or target.entry != "operator":
            raise ValueError("operator node target missing")
        workspace = DifyWorkspaces(database).resolve(binding.tenant_id, settings.environment)
        result = ensure_node_provider(
            DifyConsoleAdmin(
                ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
            ),
            workspace,
            gateway_url,
            json.loads(Path(target.manifest_file).read_text()),
        )
    except (ValueError, RuntimeError, OSError, StopIteration) as exc:
        typer.secho("CPMS节点工具登记未完成，请核对目标、工作区和已有定义", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


@admin_app.command("sync-knowledge-datasets")
def sync_knowledge_datasets_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    workspace: Annotated[
        str, typer.Option("--workspace", help="该租户的 Dify workspace id；缺省按 (租户, 环境) 解析")
    ] = "",
    api_id: Annotated[
        str,
        typer.Option("--api-id", help="该 workspace 的「外部知识库 API」模板 id（开户时建的，不猜）"),
    ] = "",
    name_override: Annotated[
        str,
        typer.Option(
            "--name",
            help="覆盖显示名：`<ragflow 库 id>=<名字>`，逗号分隔。用于库名超过 40 字节或撞名",
        ),
    ] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只打印将发生的动作，不发任何写请求")] = False,
    retire_orphans: Annotated[
        bool,
        typer.Option(
            "--retire",
            help="RAGFlow 里已删的库，把 Dify 那张 dataset 也删掉（**不可逆**）。默认只报告",
        ),
    ] = False,
    db: Annotated[Path | None, typer.Option("--db", help="gateway 数据库文件")] = None,
) -> None:
    """把某租户在 RAGFlow 名下的**每个库**同步成 Dify 里的一条 external dataset（#1001）。

    幂等：按 ``(workspace, external_knowledge_id)`` 查重，跑两次不新建。库名变了只改名，
    不改 id。

    RAGFlow 里已删的库：**默认只报告**（`orphans`），加 ``--retire`` 才真的删 Dify 那条
    dataset。默认关是因为手工跑这条命令时，上游读失败与"库真的没了"看起来一样 ——
    而删是不可逆的。自动同步那条路（弹窗触发）是开的，因为它刚刚读过上游、知道列表是好的。

    退出码：0 成功（含全无变化）/ 1 有拒绝或失败 / 2 缺配置。
    """
    from aiops_diagnostics.dify_console_admin import ConsoleApiError, ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify_knowledge_datasets import (
        SyncError,
        libraries_for,
    )
    from aiops_diagnostics.dify_knowledge_datasets import (
        run as sync_run,
    )
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable

    settings = _settings_with_config_file(ctx)
    database = db or settings.database_file
    if db is None and not os.environ.get("AIOPS_GATEWAY_DATABASE_FILE"):
        typer.secho(
            "缺少 --db：缺省会静默回退 XDG 默认并新建空库。请显式给出 --db <gateway.db>，"
            "或 export AIOPS_GATEWAY_DATABASE_FILE=<gateway.db>。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    missing = [
        label
        for label, value in (
            ("AIOPS_GATEWAY_ENVIRONMENT", settings.environment),
            ("AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL", settings.dify_console_base_url),
            ("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY", settings.dify_console_api_key),
            ("AIOPS_GATEWAY_KB_SERVICE_BASE_URL", settings.kb_service_base_url),
        )
        if not value
    ]
    if missing:
        typer.secho(
            "缺少 " + " / ".join(missing) + "：同步要部署身份、控制台凭据与 RAGFlow 清单的来源",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    store = GatewayStore(database)
    # 三件事各自可区分：没开户、workspace 解析不出来、RAGFlow 清单读不到。压成一句
    # "同步失败"会让人回头查三张表。
    if store.get_ragflow_tenant(tenant, settings.environment) is None:
        typer.secho(
            f"租户 {tenant} 在 {settings.environment} 没有开户（ragflow_tenants 无行）。"
            "先跑 `aiops-admin onboard-tenant`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    tenant_workspace = workspace
    if not tenant_workspace:
        try:
            tenant_workspace = DifyWorkspaces(database).resolve(tenant, settings.environment)
        except Exception as exc:  # noqa: BLE001 - 四个可区分的码在异常消息里
            typer.secho(f"workspace 解析失败：{exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    if not api_id:
        typer.secho(
            "缺少 --api-id：外部知识库 API 模板是每个 workspace 一份、由开户流程建的。"
            "不给就不猜 —— 猜一个模板等于把 dataset 绑到别处。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        rows = KbServiceClient(settings.kb_service_base_url, tenant_id=tenant).list_knowledge_bases()
    except KnowledgeSearchUnavailable as exc:
        typer.secho(
            f"读不到租户 {tenant} 在 RAGFlow 的库清单：{exc}。同步的前提是上游可达；"
            "没有清单就无从判断该建哪几条 dataset",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    libraries = libraries_for(tenant, rows)
    overrides = _name_overrides(name_override)
    if overrides:
        libraries = [
            dataclasses.replace(lib, name=overrides.get(lib.dataset_id, lib.name)) for lib in libraries
        ]

    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )
    try:
        outcome = sync_run(
            workspace_id=tenant_workspace,
            libraries=libraries,
            console=console,
            external_knowledge_api_id=api_id,
            dry_run=dry_run,
            retire_orphans=retire_orphans,
            log=lambda line: typer.secho(line, err=True),
        )
    except (SyncError, ConsoleApiError) as exc:
        code = getattr(exc, "code", "SYNC_FAILED")
        message = getattr(exc, "message", str(exc))
        typer.secho(json.dumps({"error": code, "message": message}, ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(outcome, ensure_ascii=False))
    if outcome.get("orphans"):
        typer.secho(
            f"注意：{len(outcome['orphans'])} 条 dataset 指向的库在 RAGFlow 里已不存在："
            + "、".join(outcome["orphans"])
            + "。**没有删除**它们 —— 删 dataset 不可逆，且可能破坏引用它的智能体。请人工决定",
            fg=typer.colors.YELLOW,
            err=True,
        )
    raise typer.Exit(code=0)


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


#: CLI 参数名 → `GatewayServerSettings` 字段。放在函数外，字典只写一次 —— 上面那个循环
#: 复制五份 `setattr` 字符串是"同一规则两份实现"的经典起点。
_SETTING_FOR = {
    "AIOPS_GATEWAY_ENVIRONMENT": "environment",
    "AIOPS_GATEWAY_DIFY_INNER_API_BASE_URL": "dify_inner_api_base_url",
    "AIOPS_GATEWAY_DIFY_INNER_API_KEY": "dify_inner_api_key",
    "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": "dify_console_base_url",
    "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": "dify_console_api_key",
    "AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY": "dify_workspace_model_api_key",
    "AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_ENDPOINT": "dify_workspace_model_endpoint",
    "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY": "ragflow_credential_key",
    "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL": "ragflow_embedding_base_url",
    "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY": "ragflow_embedding_api_key",
}
