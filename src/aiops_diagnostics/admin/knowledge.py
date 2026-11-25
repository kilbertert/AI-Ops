"""知识库委托、三元组检查、租户开户与数据集同步运维命令。"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from aiops_diagnostics.admin import common
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.knowledge.tenant_scope import namespace_for


def knowledge_delegation_command(
    context: typer.Context,
    environment: Annotated[str, typer.Option("--environment", help="目标环境")],
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户编号")],
    app: Annotated[str, typer.Option("--app", help="已登记的客服应用")],
    library: Annotated[str, typer.Option("--library", help="已明确授权的单库")],
    output: Annotated[Path, typer.Option("--output", help="新的0600私有能力文件")],
) -> None:
    """只导出现有知识授权，不授予新权限、不发布或启用。秘密不输出到终端。"""
    from aiops_diagnostics.common.private_files import write_private_text
    from aiops_diagnostics.knowledge import knowledge_delegation as delegation

    try:
        if output.exists() or output.is_symlink() or not output.is_absolute():
            raise ValueError("new private output required")
        settings = common._settings_with_config_file(context)
        capability = delegation.issue(settings, environment, tenant, app, library)
        from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint

        base = require_secure_endpoint(settings.public_base_url, what="knowledge origin")
        from urllib.parse import urlsplit

        if urlsplit(base).path or any(p.is_symlink() for p in output.parents):
            raise ValueError("fixed origin and private output required")
        body = {"schema": "aiops-knowledge-delegation/1", "base_url": base, "capabilities": [capability]}
        write_private_text(output, json.dumps(body) + "\n")
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(json.dumps({"error": "KNOWLEDGE_DELEGATION_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps({"result": "exported", "environment": environment, "tenant_id": tenant, "app_id": app})
    )


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
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
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

    gateway_schema.startup(database)
    results = check(
        store=GatewayStore(database), environment=settings.environment, dify_workspace_names=names
    )
    typer.echo(render(results))
    raise typer.Exit(code=0 if all(item.ok for item in results) else 1)


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

    from aiops_diagnostics.dify.dify_knowledge_migration import plan

    try:
        dataset_rows = _json.loads(datasets)
        api_rows = _json.loads(apis)
    except _json.JSONDecodeError as exc:
        typer.secho(f"输入不是合法 JSON：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    result = plan(dataset_rows, api_rows, workspace_id=workspace)
    typer.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    typer.secho("以上是计划，没有写任何东西。搬完之后用同一份输入跑 verify。", err=True)


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
    from aiops_diagnostics.dify.dify_ensure_workspace import (
        PLATFORM_OWNER_EMAIL,
        EnsureError,
    )
    from aiops_diagnostics.dify.dify_ensure_workspace import (
        ensure_workspace as _ensure_workspace,
    )
    from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiSettings
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import KbAdminClient, OnboardError, onboard_tenant

    settings = common._onboarding_settings_or_exit(ctx, needs_embedding=True)
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
    gateway_schema.startup(database)
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
            kb=KbAdminClient(settings.kb_service_base_url, environment=namespace_for(settings)),
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

    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import (
        KbAdminClient,
        OnboardError,
        adopt_existing_account,
    )

    settings = common._onboarding_settings_or_exit(ctx, needs_embedding=False)
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
    gateway_schema.startup(database)
    try:
        outcome = adopt_existing_account(
            tenant_id=tenant,
            environment=settings.environment,
            store=GatewayStore(database),
            kb=KbAdminClient(settings.kb_service_base_url, environment=namespace_for(settings)),
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


def rotate_ragflow_credential_command(
    ctx: typer.Context,
    tenant: Annotated[str, typer.Option("--tenant", help="公司租户 id（sys_organ.id）")],
    db: Annotated[Path | None, typer.Option("--db", help="gateway 数据库文件")] = None,
) -> None:
    """换掉一个租户的 RAGFlow 密码（#924）。旧密码立即失效。

    独立命令而不是开户的一个开关：开户必须保持"再跑一次什么都不变"，轮换不能是它的副作用。
    """
    from aiops_diagnostics.dify.dify_inner_api import InnerApiError  # noqa: F401 - 保持帮助一致的导入形状
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.kb_onboarding import KbAdminClient, OnboardError, rotate_credential

    settings = common._onboarding_settings_or_exit(ctx, needs_embedding=False)
    database = db or settings.database_file
    gateway_schema.startup(database)
    try:
        result = rotate_credential(
            tenant_id=tenant,
            environment=settings.environment,
            store=GatewayStore(database),
            kb=KbAdminClient(settings.kb_service_base_url, environment=namespace_for(settings)),
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
    from aiops_diagnostics.dify.dify_console_admin import (
        ConsoleApiError,
        ConsoleApiSettings,
        DifyConsoleAdmin,
    )
    from aiops_diagnostics.dify.dify_knowledge_datasets import (
        SyncError,
        libraries_for,
    )
    from aiops_diagnostics.dify.dify_knowledge_datasets import (
        run as sync_run,
    )
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable

    settings = common._settings_with_config_file(ctx)
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

    gateway_schema.startup(database)
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
        rows = KbServiceClient(
            settings.kb_service_base_url, tenant_id=tenant, environment=namespace_for(settings)
        ).list_knowledge_bases()
    except KnowledgeSearchUnavailable as exc:
        typer.secho(
            f"读不到租户 {tenant} 在 RAGFlow 的库清单：{exc}。同步的前提是上游可达；"
            "没有清单就无从判断该建哪几条 dataset",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    libraries = libraries_for(tenant, rows)
    overrides = common._name_overrides(name_override)
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


def register(app: typer.Typer) -> None:
    app.command("knowledge-delegation")(knowledge_delegation_command)
    app.command("check-knowledge-triple")(check_knowledge_triple_command)
    app.command("plan-knowledge-migration")(plan_knowledge_migration_command)
    app.command("onboard-tenant")(onboard_tenant_command)
    app.command("adopt-ragflow-tenant")(adopt_ragflow_tenant_command)
    app.command("rotate-ragflow-credential")(rotate_ragflow_credential_command)
    app.command("sync-knowledge-datasets")(sync_knowledge_datasets_command)
