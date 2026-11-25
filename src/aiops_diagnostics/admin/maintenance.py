"""数据库 schema、快捷动作迁移、文案管理与环境收敛运维命令。"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from aiops_diagnostics.admin import common
from aiops_diagnostics.common.config import Settings, selected_config_file
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.knowledge.tenant_scope import namespace_for


def schema_command(
    action: Annotated[str, typer.Argument(help="check（只读）或 migrate（原子升级）")],
    db: Annotated[Path, typer.Option("--db", dir_okay=False, help="明确指定 Gateway SQLite 文件")],
) -> None:
    """检查或升级全库结构；只输出版本、结构告警，不读取业务值。"""
    import sqlite3

    if action not in {"check", "migrate"}:
        raise typer.BadParameter("action 只能是 check 或 migrate")
    try:
        if action == "migrate":
            gateway_schema.ensure(db)
        report = gateway_schema.check(db)
    except (gateway_schema.SchemaError, sqlite3.Error, OSError) as exc:
        # SQLite 错误可能携带用户构造的 SQL；输出稳定分类而非原始异常。
        typer.echo(json.dumps({"error": "SCHEMA_REFUSED", "type": type(exc).__name__}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, ensure_ascii=False))


def _platform_migration_context() -> Any:
    from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="platform-shortcut-migration", tenant_id="platform")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="platform",
        data_scope=DataScope(type="all"),
        roles=frozenset({"ROLE_PLATFORM_ADMIN"}),
        # 合成上下文：断言就是代码写下的这组能力，记 ASSERTED 档（#1040）。
        permissions=frozenset({"aiops:shortcuts:manage"}),
        capability_source=CAPABILITY_ASSERTED,
    )


def migrate_shortcuts(
    db: Annotated[Path, typer.Option("--db", exists=True, dir_okay=False, help="gateway 数据库文件")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只输出计划，零写入")] = False,
) -> None:
    """将租户复制的快捷动作幂等收敛为平台默认 + 租户覆盖。"""
    from aiops_diagnostics.publication.shortcut_lifecycle import ShortcutManager, ShortcutStore
    from aiops_diagnostics.publication.shortcut_migration import migrate_shortcuts as run_migration

    gateway_schema.startup(db)
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


def consumer_copy(
    action: Annotated[str, typer.Argument(help="publish 或 show")],
    tenant: Annotated[str, typer.Option("--tenant", help="租户 id（= 会话里的 tenantId）")],
    db: Annotated[Path, typer.Option("--db", exists=True, dir_okay=False, help="gateway 数据库文件")],
    file: Annotated[
        Path | None, typer.Option("--file", exists=True, dir_okay=False, help="文案包 JSON（publish 用）")
    ] = None,
    version: Annotated[int | None, typer.Option("--version", help="show 指定版本，缺省为当前版本")] = None,
    published_by: Annotated[str, typer.Option("--by", help="发布者标识，写进台账")] = "aiops-admin",
) -> None:
    """查看或发布某租户的 C 端文案包。

    文案包只能覆盖**内置文案里已有的键**，且占位符必须与内置一致 —— 少了 `{order_no}`
    就等于把订单号从答复里删掉，那种"改措辞"是破坏可核对性（ADR-0183）。
    """
    from aiops_diagnostics.consumer.presentation import CopyBundleInvalid
    from aiops_diagnostics.consumer.presentation import validate_copy_bundle as validate
    from aiops_diagnostics.gateway.gateway_store import GatewayStore

    gateway_schema.startup(db)
    store = GatewayStore(db)
    if action == "show":
        resolved, bundle = store.consumer_copy(tenant, version)
        typer.echo(
            json.dumps(
                {"tenant_id": tenant, "version": resolved, "bundle": bundle},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if action != "publish":
        raise typer.BadParameter("action 只能是 publish 或 show")
    if file is None:
        raise typer.BadParameter("publish 需要 --file")
    raw = json.loads(file.read_text(encoding="utf-8"))
    try:
        bundle = {"languages": validate(raw)}
    except CopyBundleInvalid as exc:
        typer.echo(json.dumps({"published": False, "reason": str(exc)}, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    resolved = store.publish_consumer_copy(tenant, bundle, published_by=published_by)
    typer.echo(
        json.dumps(
            {
                "tenant_id": tenant,
                "version": resolved,
                "languages": sorted(bundle["languages"]),
                "published_by": published_by,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _reconcile_settings(config_file: Path | None) -> Settings:
    path = config_file if config_file is not None else selected_config_file()
    return Settings.from_config(path)


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
    from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry
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
    settings = common._reconcile_settings(ctx.obj.get("config_file") if ctx.obj else None)
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

    gateway_schema.startup(database)
    knowledge_resolver = None
    if kb_url:
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


def register(app: typer.Typer) -> None:
    app.command("schema")(schema_command)
    app.command("migrate-shortcuts")(migrate_shortcuts)
    app.command("consumer-copy")(consumer_copy)
    app.command("reconcile")(reconcile)
