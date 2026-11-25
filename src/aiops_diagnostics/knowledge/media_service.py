"""签名媒体资源代理与活性复核服务（`/v1/media`）。

从 `gateway_runtime.GatewayRuntime.serve_media` 拆出，将多层嵌套闭包拆分为独立的
活性校验与按租户拉取函数，明确区分四类签名来源：
- `knowledge_delegation` 委派检索媒体
- `consumer:` C 端正式服务运行产物媒体
- `candidate_knowledge` 候选草稿试运行媒体
- 已发布 Agent 版本 / `dify:` 知识检索媒体
"""

from __future__ import annotations

from typing import Any

from aiops_diagnostics.execution.agent_runs import scope_from_json
from aiops_diagnostics.identity.order_visibility import tenant_matches
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchUnavailable,
    MediaAccessDenied,
    MediaGrant,
    MediaProxy,
    MediaResponse,
)
from aiops_diagnostics.publication.agent_serviceability import live_version


def _is_delegation_media_active(runtime: Any, grant: MediaGrant) -> bool:
    from aiops_diagnostics.knowledge import knowledge_delegation

    try:
        knowledge_delegation.media_active(runtime.gateway_settings, grant)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _is_consumer_media_active(runtime: Any, grant: MediaGrant) -> bool:
    run = runtime.dify_run_store.by_qa(grant.session_id or "")
    if run is None or run.kind != "service":
        return False
    from aiops_diagnostics.consumer.consumer_chat_service import require_result

    try:
        caller = scope_from_json(run.scope_json)
        qa = runtime.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
        if (
            qa is None
            or qa["status"] != "completed"
            or run.order_no
            or not tenant_matches(grant.tenant_id, run.tenant_id)
        ):
            return False
        require_result(runtime, caller, qa)
        snapshot = runtime.dify_run_store.snapshot(run.run_id) or {}
        return any(
            block.get("media", {}).get("resource_id") == grant.resource_id
            for block in snapshot.get("blocks", [])
        )
    except Exception:
        return False


def _is_candidate_media_active(runtime: Any, grant: MediaGrant) -> bool:
    from aiops_diagnostics.dify import candidate_knowledge

    try:
        scope = candidate_knowledge.parse_id(
            grant.agent_version.removeprefix(candidate_knowledge.MEDIA_PREFIX)
        )
        candidate_knowledge.active(runtime.gateway_settings, scope)
        if grant.session_id is not None:
            runtime.consumer_candidate_trials.authorize_media(grant, scope)
        return (
            tenant_matches(grant.tenant_id, candidate_knowledge.namespace(scope))
            and grant.knowledge_base_id == scope[3]
        )
    except (ValueError, OSError, RuntimeError, AttributeError, KeyError, TypeError):
        return False


def _is_published_agent_media_active(runtime: Any, grant: MediaGrant) -> bool:
    if runtime.agent_store is None:
        return False
    agent_id, _, version_part = str(grant.agent_version).partition("#v")
    if not agent_id or not version_part.isdigit():
        return str(grant.agent_version).startswith("dify:")
    try:
        published = runtime.agent_store.get(agent_id, grant.tenant_id)
        version = runtime.agent_store.version(agent_id, grant.tenant_id, int(version_part))
        current = runtime.agent_store.version(agent_id, grant.tenant_id, published.published_version)
    except Exception:
        return False
    if live_version(published) is None:
        return False
    signed_kbs = tuple(version.snapshot.get("knowledge_base_ids") or ())
    current_kbs = tuple(current.snapshot.get("knowledge_base_ids") or ())
    return grant.knowledge_base_id in signed_kbs and grant.knowledge_base_id in current_kbs


def is_media_grant_active(runtime: Any, grant: MediaGrant) -> bool:
    """复核签发媒体链接的代理/委派/试运行在读取时刻是否仍然有效。"""
    from aiops_diagnostics.dify import candidate_knowledge
    from aiops_diagnostics.knowledge import knowledge_delegation

    if grant.agent_version.startswith(knowledge_delegation.MEDIA_PREFIX):
        return _is_delegation_media_active(runtime, grant)
    if grant.agent_version.startswith("consumer:"):
        return _is_consumer_media_active(runtime, grant)
    if grant.agent_version.startswith(candidate_knowledge.MEDIA_PREFIX):
        return _is_candidate_media_active(runtime, grant)
    return _is_published_agent_media_active(runtime, grant)


def fetch_media_for_tenant(
    runtime: Any,
    grant: MediaGrant,
    *,
    range_header: str | None = None,
) -> bytes | MediaResponse:
    """按 grant 所属租户与来源路径拉取媒体字节，并在拉取后复核活性。"""
    from aiops_diagnostics.dify.candidate_knowledge import MEDIA_PREFIX
    from aiops_diagnostics.knowledge import knowledge_delegation

    if getattr(runtime.gateway_settings, "knowledge_delegation_file", ""):
        from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application

        run = runtime.dify_run_store.by_qa(grant.session_id or "")
        if run is None or not grant.agent_version.startswith("consumer:"):
            raise MediaAccessDenied("delegated media requires current consumer run")
        bound = for_application(runtime, grant.tenant_id, run.app_id)
        body = bound.fetch_media(grant, range_header=range_header)
        if not is_media_grant_active(runtime, grant):
            raise MediaAccessDenied("consumer media is no longer active")
        return body

    if grant.agent_version.startswith(knowledge_delegation.MEDIA_PREFIX):
        try:
            scope = knowledge_delegation.media_active(runtime.gateway_settings, grant)
            body = knowledge_delegation._client(runtime.gateway_settings, scope).fetch_media(
                grant,
                range_header=range_header,
                include_response=True,
                max_bytes=32 * 1024 * 1024,
            )
            knowledge_delegation.media_active(runtime.gateway_settings, grant)
            return body
        except knowledge_delegation.DelegationRefused as exc:
            raise MediaAccessDenied("knowledge delegation revoked") from exc

    client = runtime.kb_search_client
    if grant.agent_version.startswith(MEDIA_PREFIX):
        bound: Any = KbServiceClient(
            runtime.gateway_settings.kb_service_base_url,
            tenant_id=grant.tenant_id,
            timeout=runtime.gateway_settings.kb_service_timeout_seconds,
        )
    else:
        bound = client.for_tenant(grant.tenant_id) if hasattr(client, "for_tenant") else client

    body = (
        bound.fetch_media(grant, range_header=range_header, include_response=True)
        if isinstance(bound, KbServiceClient)
        else bound.fetch_media(grant)
        if range_header is None
        else bound.fetch_media(grant, range_header=range_header)
    )
    # WF01 素材取回期间可能撤回或过期，读取前的检查不能覆盖这段等待。
    if grant.agent_version.startswith(("consumer:", MEDIA_PREFIX)) and not is_media_grant_active(
        runtime, grant
    ):
        raise MediaAccessDenied("consumer media is no longer active")
    return body


def serve_media(
    runtime: Any,
    signed_id: str,
    *,
    range_header: str | None = None,
) -> MediaResponse | None:
    """服务签名媒体请求；未配置媒体面时返回 ``None``（由 API 层转统一 404）。"""
    if runtime.media_signer is None or (
        runtime.kb_search_client is None
        and not getattr(runtime.gateway_settings, "knowledge_delegation_file", "")
    ):
        return None

    if runtime._media_proxy is None:
        runtime._media_proxy = MediaProxy(
            runtime.media_signer,
            lambda grant, *, range_header=None: fetch_media_for_tenant(
                runtime, grant, range_header=range_header
            ),
        )

    try:
        return runtime._media_proxy.serve_signed(
            signed_id,
            range_header=range_header,
            is_active=lambda grant: is_media_grant_active(runtime, grant),
        )
    except KnowledgeSearchUnavailable:
        return MediaResponse(503, {"Cache-Control": "no-store"}, b"")
