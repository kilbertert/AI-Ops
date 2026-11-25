"""发布后的正式路由指针推进原语，供网关控制台、自助开通与环境正式发布共用，避免反向依赖 gateway_api。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import status

from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.gateway.errors import StandardAPIError


def _declared_in_file(settings: Any, environment: str, tenant_id: str, entry: str, scenario: str) -> bool:
    """这条 key 是不是仍由**文件登记**声明。

    启动期查过一次冲突，但那之后文件可能被重新配上；而发布是唯一会把冲突**写进去**
    的动作，所以它也要查一次 —— 写进去之后再发现，故障是整表加载失败，波及所有租户。
    """
    try:
        bindings = chat_execution.load_bindings(
            str(getattr(settings, "chat_execution_bindings_file", "") or "")
        )
    except chat_execution.ChatBindingError as exc:
        raise StandardAPIError(503, "EXEC_CONFIG_UNAVAILABLE", "bindings unavailable") from exc
    key = f"{scenario}:{environment}:{tenant_id}"
    return any(item.key == key for item in bindings)


def _key_ref_for(key_dir: str, app_id: str) -> str:
    """这个 app 的 Service API 凭据路径，且**必须可读**。

    发布只推路径、不读密钥；但一条指向不存在文件的路径等于"发布了也跑不起来"。
    """
    from aiops_diagnostics.dify.dify_chat_workflow import load_workflow_key

    if not key_dir:
        return ""
    path = chat_execution.key_file_for_app(key_dir, app_id)
    try:
        load_workflow_key(path)
    except ValueError as exc:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_MISSING",
            "the app service key is not provisioned on this server",
        ) from exc
    return path


def advance_route_binding(
    context: Any,
    outcome: Any,
    binding: Any,
    *,
    initiator: tuple[str, str, str],
    expected_revision: int | None = None,
) -> None:
    """发布成功之后，把（环境，租户，入口，场景）的正式指针推到这一版。

    **拒绝的三种情况**，都不写指针、并如实报未完成：

    * 没有配 `AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE`：这个环境还没接管管理指针，
      发布只冻结版本（与今天逐字一致）；
    * `version_no` 缺失或 `workflow_id` 取不到：拿不到"哪一版、哪张图"就没有可切的东西；
    * 已有发布者在本轮期间改过指针（`revision` 变化）：CAS 失败，不覆盖别人的发布。

    成功时写入的身份是**三列分开**的：`app_id` 是应用、`workflow_id` 是图、
    版本是本地那一条快照。把同一个值填进多列，等于三列都在撒谎。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    environment = str(getattr(context.settings, "execution_environment", "") or "")
    if not database or not environment:
        return
    if not getattr(outcome, "workflow_id", ""):
        return
    version_no = outcome.version_no
    if not isinstance(version_no, int):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_ROUTE_NOT_ADVANCED",
            "the published version has no workflow identity to bind",
        )

    from aiops_diagnostics.consumer.consumer_publication import route_scenario
    from aiops_diagnostics.execution.chat_route_bindings import (
        ChatRouteBindingStore,
        RouteBindingError,
    )

    key_dir = str(getattr(context.settings, "chat_dify_key_dir", "") or "")
    entry = str(getattr(binding, "business_entry", "") or "consumer")
    scenario = getattr(outcome, "route_scenario", "") or route_scenario(context.settings, binding)
    if not key_dir:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_DIR_UNSET",
            "the app service key directory is not configured on this server",
        )
    if not str(getattr(context.settings, "dify_chat_base_url", "") or ""):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_CHAT_BASE_URL_UNSET",
            "the dify chat base url is not configured on this server",
        )
    if _declared_in_file(context.settings, environment, binding.tenant_id, entry, scenario):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CHAT_ROUTE_BINDING_COLLIDES_WITH_FILE",
            "this route is still declared in the bindings file",
        )
    if scenario == chat_execution.SCENARIO_CONSUMER_SERVICE and getattr(context, "agent_manager", None):
        from aiops_diagnostics.publication.agent_lifecycle import AgentError

        try:
            snapshot = context.agent_manager.store.version(
                outcome.agent_id, binding.tenant_id, version_no
            ).snapshot
        except AgentError as exc:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "CONSUMER_VERSION_UNAVAILABLE",
                "selected consumer version is unavailable",
            ) from exc
        frozen = snapshot.get("consumer_service") if isinstance(snapshot, dict) else None
        content = frozen.get("content") if isinstance(frozen, dict) else None
        if isinstance(content, dict) and content.get("environment") not in (None, environment):
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "CONSUMER_VERSION_ENVIRONMENT_MISMATCH",
                "selected version belongs to another execution environment",
            )

    store = ChatRouteBindingStore(Path(database).expanduser().absolute())
    existing = store.get(environment, binding.tenant_id, entry, scenario)
    if (
        outcome.action == "unchanged"
        and existing is not None
        and existing.agent_id == outcome.agent_id
        and existing.agent_version_no == version_no
        and existing.workflow_id == outcome.workflow_id
    ):
        if expected_revision is not None and existing.revision != expected_revision:
            raise StandardAPIError(409, "ROUTE_BINDING_CONFLICT", "route binding moved")
        if existing.active:
            return
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "ROUTE_BINDING_RETRACTED",
            "该版本已撤回；要重新上线请用「恢复历史版本」",
        )
    if expected_revision is None:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "initial route revision unavailable")
    try:
        store.publish(
            environment=environment,
            tenant_id=binding.tenant_id,
            entry=entry,
            scenario=scenario,
            engine="dify",
            agent_id=outcome.agent_id,
            agent_version_no=version_no,
            app_id=binding.app_id,
            workflow_id=outcome.workflow_id,
            workflow_key_ref=_key_ref_for(key_dir, binding.app_id),
            expected_revision=expected_revision,
            updated_by=initiator[0] or initiator[1] or "",
        )
    except RouteBindingError as exc:
        raise StandardAPIError(status.HTTP_409_CONFLICT, exc.code, "route binding was not advanced") from exc
