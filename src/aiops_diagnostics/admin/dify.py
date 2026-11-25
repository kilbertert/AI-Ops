"""Dify 控制台拉取、同步、工作区与节点提供方运维命令。"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from aiops_diagnostics.admin import common
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.knowledge.tenant_scope import namespace_for


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
    from aiops_diagnostics.dify.dify_dsl_pull import (
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

    settings = common._reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    # 控制台三键与 `admin reconcile` 读**同一个文件**；守卫与 `sync-from-dify` 共用一份，
    # 免得 #625 修的那个「`--config` 里的控制台键读不到」只对其中一条命令生效。
    console, database = common._dify_console_and_db(ctx, db)
    # 声明的 dataset 靠这张表翻译成我们的知识库（与检索路由用的是同一张、同一个解析器）。
    knowledge_registry = common._dify_knowledge_registry(console)

    knowledge_resolver = None
    if kb_url:
        from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(
                kb_url,
                tenant_id="aiops",
                timeout=10,
                environment=namespace_for(common._settings_with_config_file(ctx)),
            )
        )
    manager = AgentManager(
        AgentStore(database),
        knowledge_resolver=knowledge_resolver,
        allowed_models=allowed_models_from_settings(settings),
    )
    context = admin_context(tenant)
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    workspaces = DifyWorkspaces(database)
    resolved_workspace = common._workspace_for(workspaces, tenant, console.environment)
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
            from aiops_diagnostics.dify.dify_dsl_pull import resolve_pulled_config

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
    from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.dify.dify_dsl_pull import (
        DifyDslError,
        DifyDslSource,
        sync_binding_from_dify,
    )
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
    from aiops_diagnostics.publication.agent_lifecycle import (
        AgentManager,
        AgentStore,
        allowed_models_from_settings,
    )
    from aiops_diagnostics.publication.agent_manifest import admin_context

    console, database = common._dify_console_and_db(ctx, db)
    settings = common._reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
    knowledge_registry = common._dify_knowledge_registry(console)
    knowledge_resolver = None
    if kb_url:
        from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

        knowledge_resolver = KbBindingResolver(
            KbServiceKnowledgeClient(
                kb_url,
                tenant_id="aiops",
                timeout=10,
                environment=namespace_for(common._settings_with_config_file(ctx)),
            )
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
            resolved[binding.tenant_id] = common._workspace_for(workspaces, binding.tenant_id, environment)
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
    from aiops_diagnostics.dify.dify_ensure_workspace import (
        PLATFORM_OWNER_EMAIL,
        EnsureError,
        ensure_workspace,
        render,
    )
    from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    settings = common._settings_with_config_file(ctx)

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

    gateway_schema.startup(database)
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
    from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify.dify_tenant_access import GrantError, grant_tenant_access
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    settings = common._settings_with_config_file(ctx)
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

    gateway_schema.startup(settings.database_file)
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
    from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify.dify_ensure_workspace import EnsureError
    from aiops_diagnostics.dify.dify_workspace_ready import (
        DEFAULT_APP_NAME,
        ReadySettings,
        bundled_template,
        ensure_ready,
    )
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    settings = common._settings_with_config_file(ctx)
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

    gateway_schema.startup(database)
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


def ensure_node_provider_command(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Option("--app-id")],
    gateway_url: Annotated[str, typer.Option("--gateway-url")],
    db: Annotated[Path, typer.Option("--db", exists=True, dir_okay=False)],
    migrate: Annotated[
        bool, typer.Option("--migrate", help="已有定义与当前声明不同时就地覆盖，而不是报错")
    ] = False,
) -> None:
    """为已登记管家节点应用安装真实CPMS工具定义；不写业务凭据。

    工具集变了（新增或改名）时，provider 的 schema 一定会变。``--migrate`` 是这条变更的
    显式出路：不加它就是一次明确报错，加了它就是一次可审计的运营动作。
    """
    from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry
    from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.execution.node_publication import target_for
    from aiops_diagnostics.operator.ops_provider import ensure_node_provider

    settings, database = common._dify_console_and_db(ctx, db)
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
            migrate=migrate,
        )
    except (ValueError, RuntimeError, OSError, StopIteration) as exc:
        typer.secho("CPMS节点工具登记未完成，请核对目标、工作区和已有定义", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def register(app: typer.Typer) -> None:
    app.command("pull-dify")(pull_dify)
    app.command("sync-from-dify")(sync_from_dify)
    app.command("ensure-workspace")(ensure_workspace_command)
    app.command("grant-tenant-access")(grant_tenant_access_command)
    app.command("ensure-ready")(ensure_ready_command)
    app.command("ensure-node-provider")(ensure_node_provider_command)
