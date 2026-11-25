"""环境目录、草稿、计划、任务派发与正式发布运维命令。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from aiops_diagnostics.admin import common
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings


def environment_registry_command(
    action: Annotated[str, typer.Argument(help="check（摘要）、apply（发布）、migrate（升级管理目录）")],
    registry: Annotated[Path, typer.Option("--registry", help="独立私有目录内的登记JSON")],
    file: Annotated[Path | None, typer.Option("--file", help="环境数组JSON（0600）")] = None,
    expected_revision: Annotated[int, typer.Option("--expected-revision", help="预期版本，首次为0")] = 0,
    actor: Annotated[str, typer.Option("--by", help="审计操作人")] = "aiops-admin",
) -> None:
    """维护平台环境与租户登记；输出版本/数量，不打印地址或配置引用。"""
    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding import registry as environment_registry

    try:
        if action == "apply":
            if file is None:
                raise ValueError("apply需要--file")
            validate_private_file(file)
            if file.stat().st_size > environment_registry.MAX_BYTES:
                raise ValueError("输入过大")
            incoming = json.loads(file.read_text(), object_pairs_hook=environment_registry._unique_object)
            managers = None
            if isinstance(incoming, dict):
                if set(incoming) != {"environments", "managers"}:
                    raise ValueError("新版输入须含environments和managers")
                managers, incoming = incoming["managers"], incoming["environments"]
                if managers is None:
                    raise ValueError("managers须为数组")
            environment_registry.apply(
                registry, incoming, expected_revision=expected_revision, actor=actor, managers=managers
            )
        elif action == "migrate":
            environment_registry.migrate(registry, expected_revision=expected_revision, actor=actor)
        elif action != "check":
            raise ValueError("action只能是check、apply或migrate")
        body = environment_registry.load(registry)
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(
            json.dumps(
                {
                    "error": "ENVIRONMENT_REGISTRY_REFUSED",
                    "reason": str(exc)
                    if isinstance(exc, environment_registry.RegistryError)
                    else "ENVIRONMENT_REGISTRY_IO_FAILED"
                    if isinstance(exc, OSError)
                    else "INVALID_INPUT",
                }
            )
        )
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "revision": body["revision"],
                "environments": len(body["environments"]),
                "tenants": sum(len(r["tenants"]) for r in body["environments"]),
            }
        )
    )


def environment_adopt_execution_command(
    database: Annotated[Path, typer.Option("--database", help="Gateway SQLite 数据库路径（0600）")],
    source: Annotated[str, typer.Option("--from", help="源执行环境编号（如 production-41）")],
    target: Annotated[str, typer.Option("--to", help="目标执行环境编号（如 41）")],
    backup: Annotated[
        Path | None, typer.Option("--backup", help="独占私有备份清单路径；省略则为只读预演")
    ] = None,
    restore: Annotated[bool, typer.Option("--restore", help="按本次备份清单执行逐项 CAS 恢复")] = False,
    actor: Annotated[str, typer.Option("--by", help="审计操作人")] = "aiops-admin",
    config_ref: Annotated[
        list[Path] | None, typer.Option("--config-ref", help="可选只读检查的环境配置文件路径（可重复）")
    ] = None,
    verify_bindings: Annotated[
        bool, typer.Option("--verify-bindings", help="迁移/恢复前用真实运行时加载器核验全部活跃路由")
    ] = False,
    bindings_file: Annotated[
        Path | None,
        typer.Option("--bindings-file", help="可选静态路由绑定文件，配合 --verify-bindings 核对无重名冲突"),
    ] = None,
) -> None:
    """一次性受控执行环境迁移：默认只读预演，带 --backup 执行单事务迁移或 --restore CAS 恢复。"""
    from aiops_diagnostics.onboarding import execution_adoption as adoption

    try:
        result = adoption.adopt(
            database,
            source,
            target,
            actor=actor,
            backup=backup,
            restore=restore,
            config_files=tuple(config_ref or ()),
            verify_bindings=verify_bindings,
            bindings_file=bindings_file,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(
            json.dumps(
                {
                    "error": "ENVIRONMENT_ADOPTION_REFUSED",
                    "reason": str(exc)
                    if isinstance(exc, adoption.AdoptionError)
                    else "ADOPTION_IO_FAILED"
                    if isinstance(exc, OSError)
                    else "INVALID_INPUT",
                }
            )
        )
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_draft_command(
    registry: Annotated[Path, typer.Option("--registry", help="私有生效目录JSON")],
    file: Annotated[
        Path, typer.Option("--file", help="含identity/draft/revision/registry_revision的私有JSON")
    ],
) -> None:
    """受管恢复入口；与管理API共用草稿校验、显式授权和版本比较，不激活环境。"""
    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding import drafts as environment_drafts
    from aiops_diagnostics.onboarding import registry as directory

    try:
        validate_private_file(file)
        if file.stat().st_size > 65536:
            raise ValueError("input too large")
        data = json.loads(file.read_text(), object_pairs_hook=directory._unique_object)
        if not isinstance(data, dict) or set(data) != {"identity", "draft", "revision", "registry_revision"}:
            raise ValueError("invalid input")
        identity = data["identity"]
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or any(not isinstance(v, str) for v in identity)
        ):
            raise ValueError("invalid identity")
        result = environment_drafts.save(
            registry,
            tuple(identity),
            data["draft"],
            expected_revision=data["revision"],
            registry_revision=data["registry_revision"],
        )
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_DRAFT_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result))


def environment_plan_command(
    registry: Annotated[Path, typer.Option("--registry", help="私有生效目录")],
    file: Annotated[Path, typer.Option("--file", help="含identity和计划请求的私有JSON")],
    references: Annotated[Path | None, typer.Option("--references", help="受控部署引用清单")] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="仅编号回读时导出私有停用候选包；不安装")
    ] = None,
) -> None:
    """与管理API共用配置摘要预检或计划回读；不安装、不激活。"""
    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding import plans as environment_plans
    from aiops_diagnostics.onboarding import registry as environment_registry

    try:
        validate_private_file(file)
        if file.stat().st_size > 65536:
            raise ValueError("input too large")
        data = json.loads(file.read_text(), object_pairs_hook=environment_registry._unique_object)
        if not isinstance(data, dict):
            raise ValueError("invalid input")
        identity = data.pop("identity", None)
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or any(not isinstance(v, str) for v in identity)
        ):
            raise ValueError("invalid identity")
        if set(data) == {"id"}:
            result = (
                environment_plans.export(registry, tuple(identity), data["id"], references, output)
                if output is not None
                else environment_plans.read(registry, tuple(identity), data["id"], references)
            )
        elif set(data) == {"environment", "revision", "registry_revision"}:
            if output is not None:
                raise ValueError("export requires a frozen plan id")
            result = environment_plans.preflight(
                registry,
                tuple(identity),
                data["environment"],
                data["revision"],
                data["registry_revision"],
                references,
            )
        else:
            raise ValueError("invalid input")
    except (OSError, ValueError, RuntimeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_PLAN_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_artifacts_command(
    context: typer.Context,
    registry: Annotated[Path, typer.Option("--registry", help="计划使用的私有生效目录")],
    references: Annotated[Path, typer.Option("--references", help="0600受控部署引用文件")],
    file: Annotated[Path, typer.Option("--file", help="0600身份、CAS摘要和制品输入JSON")],
) -> None:
    """CAS更新应用/安装器或既有配置版本；不安装或激活。"""
    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding import plans as environment_plans
    from aiops_diagnostics.onboarding import registry as environment_registry

    try:
        settings = common._settings_with_config_file(context)
        configured_registry = getattr(settings, "environment_management_registry_file", "") or getattr(
            settings, "environment_registry_file", ""
        )
        if not configured_registry or Path(configured_registry).absolute() != registry.absolute():
            raise ValueError("registry path must match the installed management registry")
        if (
            not settings.environment_onboarding_file
            or Path(settings.environment_onboarding_file).absolute() != references.absolute()
        ):
            raise ValueError("references path must match the installed onboarding reference")
        validate_private_file(file)
        if file.stat().st_size > 65536:
            raise ValueError("input too large")
        data = json.loads(file.read_text(), object_pairs_hook=environment_registry._unique_object)
        if not isinstance(data, dict) or set(data) not in (
            {"identity", "environment", "expected_sha256", "application", "installer"},
            {"identity", "environment", "expected_sha256", "configuration"},
        ):
            raise ValueError("invalid input")
        identity = data["identity"]
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or any(not isinstance(value, str) for value in identity)
        ):
            raise ValueError("invalid identity")
        result = environment_plans.update_artifact_references(
            references,
            registry_path=registry,
            identity=tuple(identity),
            environment=data["environment"],
            expected_sha256=data["expected_sha256"],
            application=data.get("application"),
            installer=data.get("installer"),
            configuration=data.get("configuration"),
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_REFERENCES_UPDATE_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result))


def environment_runtime_command(
    context: typer.Context,
    file: Annotated[Path, typer.Option("--file", help="含identity、计划及双端任务编号的私有JSON")],
    node: Annotated[Path, typer.Option("--node", help="目标环境专属节点配置引用，私有JSON")],
    output: Annotated[Path, typer.Option("--output", help="全新私有运行工件；不安装或激活")],
) -> None:
    """将已观察候选资源接线为受控运行工件，秘密仅保存在工件。"""
    from aiops_diagnostics.onboarding import runtime as environment_runtime

    try:
        validate = environment_runtime._private_json(file)[0]
        settings = common._settings_with_config_file(context)
        result = environment_runtime.export(settings, validate, node, output)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_RUNTIME_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_runtime_register_command(
    context: typer.Context,
    sha256: Annotated[str, typer.Option("--sha256", help="现役运行工件的受管摘要")],
    receipt_fd: Annotated[int, typer.Option("--receipt-fd", help="仅安装器传入的只读账本描述符")],
    observe_only: Annotated[bool, typer.Option("--observe-only", help="只读核对，缺失时不补登记")] = False,
) -> None:
    """登记已安装的停用工作区与双端应用，不发布或启用。"""
    from aiops_diagnostics.onboarding.runtime import register_installed

    try:
        result = register_installed(
            common._settings_with_config_file(context), sha256, receipt_fd, observe_only=observe_only
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_RUNTIME_REGISTRATION_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_task_worker(
    task_id: Annotated[str | None, typer.Option("--id", help="指定编号；省略自动领取一个待处理预检")] = None,
    gateway: Annotated[str | None, typer.Option("--gateway", help="获准中央HTTPS或本机回环入口")] = None,
    key_file: Annotated[Path | None, typer.Option("--key-file", help="独立内部HMAC密钥文件0600")] = None,
    central_target: Annotated[
        str | None, typer.Option("--central-target", help="受管中央档案；不传执行密钥")
    ] = None,
) -> None:
    """受管运维主机单次核对现场；不在业务Gateway运行，不安装或激活。"""
    import subprocess

    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

    try:
        key = ""
        if central_target:
            if gateway is not None or key_file is not None:
                raise ValueError("central transport cannot carry a key or URL")
        else:
            if gateway is None or key_file is None:
                raise ValueError("gateway and key required")
            validate_private_file(key_file)
            key = key_file.read_text().strip()
            if not key or key_file.stat().st_size > 4096:
                raise ValueError("invalid worker key")
        root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
        from aiops_diagnostics.onboarding import task_ssh_runner as worker

        result = worker.run(
            root,
            canonical_gateway_url(gateway) if gateway else "",
            key,
            task_id,
            central_target=central_target,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_TASK_WORKER_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_task_dispatch(context: typer.Context) -> None:
    """中央固定身份接收stdin任务JSON；配置/密钥不返回运维宿主。"""
    _environment_dispatch(context)


def environment_trial_dispatch(context: typer.Context) -> None:
    """受管服务身份向本机候选入口转交stdin；公司会话仅交目标。"""
    _environment_dispatch(context, trial=True)


def _environment_dispatch(context, *, trial=False):
    import http.client
    import pwd
    import socket
    import sys

    from dotenv import dotenv_values

    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding.task_ssh_runner import call

    try:
        config = context.obj.get("config_file")
        import re

        match = re.fullmatch(r"/etc/aiops-access-([a-z0-9][a-z0-9_-]{0,31})/production.env", str(config))
        target_profile = bool(trial and match)
        environment = match[1] if target_profile else "41"
        # 目标配置由既有服务身份所有，不能让调用者指定另一身份或路径。
        user = pwd.getpwuid(config.stat().st_uid).pw_name if target_profile else "aiops41"
        if os.getuid() != pwd.getpwnam(user).pw_uid or (
            not target_profile and config != Path("/etc/aiops-41/gateway.env")
        ):
            raise ValueError("central service identity and fixed configuration required")
        validate_private_file(config)
        os.environ.update(
            {k: v for k, v in dotenv_values(config, interpolate=False).items() if v is not None}
        )
        if target_profile:
            os.environ.update(
                {
                    "AIOPS_GATEWAY_ENVIRONMENT": environment,
                    "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(config),
                    "AIOPS_GATEWAY_DATABASE_FILE": f"/var/lib/aiops-access-{environment}/gateway/gateway.db",
                }
            )
        settings = GatewayServerSettings.from_env()
        if settings.environment != environment:
            raise ValueError("central profile not configured")
        if not settings.environment_task_key:
            raise ValueError("ENVIRONMENT_VALIDATION_KEY_REQUIRED")
        # 目标只取本机配置；bind验证地址确实属于本机，不从stdin指定上游。
        host = settings.bind_host if settings.bind_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        with socket.socket() as sock:
            sock.bind((host, 0))
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("input too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        port = int(os.environ.get("AIOPS_GATEWAY_PORT", "18951")) if target_profile else settings.port
        if trial:
            import time

            from aiops_diagnostics.common.http_auth import build_internal_token_headers

            # 本机Gateway可监听桥地址；管理请求来源仍须为回环，不能放宽入口门。
            connection = http.client.HTTPConnection(
                host,
                port,
                timeout=220 if payload.get("action") == "business-step" else 30,
                source_address=("::1" if ":" in host else "127.0.0.1", 0),
            )
            try:
                connection.request(
                    "POST",
                    "/v1/internal/environment-trial",
                    body=json.dumps(payload),
                    headers={
                        **build_internal_token_headers(settings.environment_task_key, 300, int(time.time())),
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise ValueError("local candidate trial refused")
                result = json.loads(body)
            finally:
                connection.close()
        else:
            result = call(f"http://{host}:{port}", settings.environment_task_key, payload)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        code = (
            "ENVIRONMENT_VALIDATION_KEY_REQUIRED"
            if str(exc) == "ENVIRONMENT_VALIDATION_KEY_REQUIRED"
            else "ENVIRONMENT_TASK_DISPATCH_REFUSED"
        )
        typer.echo(json.dumps({"error": code}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


def environment_release_command(
    context: typer.Context,
    action: Annotated[str, typer.Argument(help="prepare/publish/observe/restore")],
    file: Annotated[Path, typer.Option("--file", help="安装器准备的私有发布工件")],
    receipt_fd: Annotated[int, typer.Option("--receipt-fd", help="root只读installed账本描述符")],
    release_fd: Annotated[int, typer.Option("--release-fd", help="root只读已批准发布工件描述符")],
) -> None:
    """仅受管安装器消费冻结内容，不访问Console管理接口。"""
    import fcntl
    import stat

    from aiops_diagnostics.common.private_files import validate_private_file
    from aiops_diagnostics.onboarding.release import context_for, operation

    settings = common._settings_with_config_file(context)
    try:
        receipt_path = Path("/opt/aiops-access-" + settings.environment + "/installed.json")
        info = os.fstat(receipt_fd)
        if (
            info.st_uid != 0
            or info.st_mode & 0o077
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or fcntl.fcntl(receipt_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
            or receipt_path.is_symlink()
            or (info.st_dev, info.st_ino) != (receipt_path.stat().st_dev, receipt_path.stat().st_ino)
        ):
            raise ValueError("root installation receipt required")
        validate_private_file(file)
        if file.stat().st_size > 1048576 or action not in {"prepare", "publish", "observe", "restore"}:
            raise ValueError("release request refused")
        approval = os.fstat(release_fd)
        if (
            approval.st_uid != 0
            or approval.st_mode & 0o077
            or not stat.S_ISREG(approval.st_mode)
            or approval.st_nlink != 1
            or fcntl.fcntl(release_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        ):
            raise ValueError("root approved artifact required")
        os.lseek(release_fd, 0, os.SEEK_SET)
        approved = os.read(release_fd, 1048577)
        if len(approved) > 1048576 or file.read_bytes() != approved:
            raise ValueError("approved artifact changed")
        document = json.loads(approved)
        result = operation(context_for(settings), document, action)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        typer.echo(json.dumps({"error": "ENVIRONMENT_RELEASE_REFUSED"}))
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result))


def register(app: typer.Typer) -> None:
    app.command("environment-registry")(environment_registry_command)
    app.command("environment-adopt-execution")(environment_adopt_execution_command)
    app.command("environment-draft")(environment_draft_command)
    app.command("environment-plan")(environment_plan_command)
    app.command("environment-artifacts")(environment_artifacts_command)
    app.command("environment-runtime")(environment_runtime_command)
    app.command("environment-runtime-register")(environment_runtime_register_command)
    app.command("environment-task-worker")(environment_task_worker)
    app.command("environment-task-dispatch")(environment_task_dispatch)
    app.command("environment-trial-dispatch")(environment_trial_dispatch)
    app.command("environment-release")(environment_release_command)
