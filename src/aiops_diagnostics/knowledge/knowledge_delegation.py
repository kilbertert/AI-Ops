"""正式知识读能力；授权真值仍是原KnowledgeGrants与应用归属。"""

import base64
import hashlib
import hmac
import json
import math

from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces, _identifier
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchUnavailable,
    normalize_search_response,
)
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant
from aiops_diagnostics.onboarding.drafts import REFERENCE_PATTERN

PREFIX = "kdx_"
MEDIA_PREFIX = "dify-delegated:"


class DelegationRefused(ValueError):
    pass


def identifier(scope):
    if not isinstance(scope, (list, tuple)) or len(scope) != 6:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    environment, tenant, workspace, app, library, generation = scope
    if (
        not all(isinstance(v, str) for v in scope[:5])
        or canonical_environment(environment) != environment
        or not REFERENCE_PATTERN.fullmatch(tenant)
        or any(_identifier(v, "knowledge scope") != v for v in (workspace, app, library))
        or type(generation) not in (int, float)
        or not math.isfinite(generation)
        or generation <= 0
    ):
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    encoded = base64.urlsafe_b64encode(json.dumps(scope, separators=(",", ":")).encode()).decode().rstrip("=")
    if len(encoded) > 1200:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    return PREFIX + encoded


def parse_id(value):
    if not isinstance(value, str) or not value.startswith(PREFIX) or len(value) > 1204:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    try:
        encoded = value[len(PREFIX) :]
        scope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if identifier(scope) != value:
            raise ValueError()
        return tuple(scope)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED") from exc


def credential(secret, scope):
    if not secret:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    value = identifier(scope)
    signature = hmac.new(secret.encode(), ("knowledge-delegation:1:" + value).encode(), hashlib.sha256)
    return value + "." + signature.hexdigest()


def parse_token(secret, presented):
    if not isinstance(presented, str) or len(presented) > 1300:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    try:
        value, _ = presented.rsplit(".", 1)
        scope = parse_id(value)
        if not hmac.compare_digest(credential(secret, scope).encode(), presented.encode()):
            raise ValueError()
        return scope
    except (ValueError, UnicodeError) as exc:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED") from exc


def active(settings, scope):
    identifier(scope)
    environment, tenant, workspace, app, library, generation = scope
    store = SelfServiceStore(settings.database_file, readonly=True)
    row = store.app(app)
    spaces = DifyWorkspaces(settings.database_file)
    if (
        row is None
        or (row["environment"], row["tenant_id"], row["workspace_id"], row["entry"])
        != (environment, tenant, workspace, "consumer")
        or library not in json.loads(row["targets_json"]).get("knowledge_base_ids", [])
        or spaces.resolve(tenant, environment) != workspace
        or spaces.tenant_for_workspace(workspace, environment, across_environments=True) != tenant
        or KnowledgeGrants(settings.database_file).generation(tenant, environment, library) != generation
    ):
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")


def authorize(settings, presented):
    """中央授权源不可读时失败关闭；仅对外呈现固定凭据拒绝。"""
    import sqlite3

    try:
        scope = parse_token(settings.internal_token_secret, presented)
        active(settings, scope)
        return scope
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED") from exc


def issue(settings, environment, tenant, app, library):
    row = SelfServiceStore(settings.database_file, readonly=True).app(app)
    if row is None:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    scope = (
        environment,
        tenant,
        row["workspace_id"],
        app,
        library,
        KnowledgeGrants(settings.database_file).generation(tenant, environment, library),
    )
    active(settings, scope)
    return {"knowledge_id": identifier(scope), "token": credential(settings.internal_token_secret, scope)}


def _client(settings, scope):
    return KbServiceClient(
        settings.kb_service_base_url,
        tenant_id=scope[1],
        environment=scope[0],
        timeout=settings.kb_service_timeout_seconds,
    )


def catalogue(context, scope):
    active(context.settings, scope)
    rows = [
        r
        for r in _client(context.settings, scope).list_knowledge_bases()
        if r["knowledge_base_id"] == scope[4]
    ]
    active(context.settings, scope)
    if len(rows) != 1:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    return {
        "knowledge_items": [
            {"knowledge_id": identifier(scope), "name": rows[0]["name"], "knowledge_base_ids": [scope[4]]}
        ],
        "total": 1,
    }


def retrieve(context, scope, request):
    from aiops_diagnostics.dify.dify_knowledge_api import dify_records

    if request.knowledge_id != identifier(scope):
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    catalogue(context, scope)
    if context.runtime.media_signer is None:
        raise KnowledgeSearchUnavailable("knowledge media unavailable")
    maximum = request.top_k or context.settings.kb_search_max_results
    raw = _client(context.settings, scope).search((scope[4],), request.query, maximum)
    active(context.settings, scope)
    chunks = normalize_search_response(
        raw,
        tenant_id=knowledge_tenant(scope[1], scope[0]),
        agent_version=MEDIA_PREFIX + identifier(scope),
        session_id=None,
        knowledge_base_ids=(scope[4],),
        media_signer=context.runtime.media_signer,
        max_results=maximum,
    )
    records = dify_records(
        chunks, score_threshold=request.score_threshold, public_base_url=context.settings.public_base_url
    )
    indexed = {chunk.reference_id: chunk for chunk in chunks}
    for record in records:
        chunk = indexed[record["metadata"]["reference_id"]]
        for rendered, media in zip(record["metadata"]["media"], chunk.media, strict=True):
            rendered.update(resource_id=media.resource.resource_id, reference_id=chunk.reference_id)
    return {"records": records}


def media_active(settings, grant):
    scope = parse_id(grant.agent_version.removeprefix(MEDIA_PREFIX))
    active(settings, scope)
    if grant.tenant_id != knowledge_tenant(scope[1], scope[0]) or grant.knowledge_base_id != scope[4]:
        raise DelegationRefused("KNOWLEDGE_DELEGATION_REFUSED")
    return scope
