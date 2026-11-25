"""候选连接凭据：仅命名明确的环境、租户、工作区与单库。"""

import base64
import hashlib
import hmac
import json
import sqlite3

from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces, _identifier
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant
from aiops_diagnostics.onboarding.drafts import REFERENCE_PATTERN

PREFIX = "ckx_"
MEDIA_PREFIX = "dify-candidate:"


class CandidateKnowledgeError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def knowledge_id(scope):
    environment, tenant, workspace, library = scope
    canonical_environment(environment)
    if not isinstance(tenant, str) or not REFERENCE_PATTERN.fullmatch(tenant):
        raise ValueError("candidate knowledge scope invalid")
    if (
        _identifier(workspace, "workspace_id") != workspace
        or _identifier(library, "knowledge_base_id") != library
    ):
        raise ValueError("candidate knowledge scope invalid")
    encoded = base64.urlsafe_b64encode(json.dumps(scope, separators=(",", ":")).encode()).decode().rstrip("=")
    identifier = PREFIX + encoded
    if len(identifier) > 512:
        raise ValueError("candidate knowledge scope too long")
    return identifier


def token(secret, scope):
    if not secret:
        raise ValueError("candidate knowledge credential unavailable")
    identifier = knowledge_id(scope)
    signature = hmac.new(
        secret.encode(), ("candidate-knowledge:1:" + identifier).encode(), hashlib.sha256
    ).hexdigest()
    return identifier + "." + signature


def parse_id(identifier):
    if not isinstance(identifier, str) or not identifier.startswith(PREFIX) or len(identifier) > 512:
        raise ValueError("candidate knowledge identity refused")
    encoded = identifier[len(PREFIX) :]
    scope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if (
        not isinstance(scope, list)
        or len(scope) != 4
        or not all(isinstance(v, str) for v in scope)
        or knowledge_id(scope) != identifier
    ):
        raise ValueError("candidate knowledge identity refused")
    return tuple(scope)


def parse_token(secret, presented):
    if not isinstance(presented, str) or len(presented) > 2200:
        raise ValueError("candidate credential refused")
    identifier, _ = presented.rsplit(".", 1)
    scope = parse_id(identifier)
    if not hmac.compare_digest(token(secret, scope).encode(), presented.encode()):
        raise ValueError("candidate credential refused")
    return scope


def connection_binding(settings):
    return hashlib.sha256(
        json.dumps(
            {
                "connection_contract": 2,
                **{
                    key: getattr(settings, key, "")
                    for key in (
                        "public_base_url",
                        "dify_console_base_url",
                        "dify_console_api_key",
                        "internal_token_secret",
                        "kb_service_base_url",
                    )
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def active(settings, scope):
    from aiops_diagnostics.onboarding.tasks import active_connection

    path = settings.environment_management_registry_file or settings.environment_registry_file
    try:
        request = active_connection(path, settings.environment_onboarding_file, scope)
        if request["binding"] != connection_binding(settings):
            raise ValueError("candidate binding changed")
        workspaces = DifyWorkspaces(settings.database_file)
        if (
            workspaces.resolve(scope[1], scope[0]) != scope[2]
            or workspaces.tenant_for_workspace(scope[2], scope[0], across_environments=True) != scope[1]
        ):
            raise ValueError("candidate workspace refused")
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise CandidateKnowledgeError(403, "DIFY_CREDENTIAL_REJECTED", "credential rejected") from exc
    return request


def namespace(scope):
    return knowledge_tenant(scope[1], scope[0])


def _owned(context, scope):
    from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable

    client = KbServiceClient(
        context.settings.kb_service_base_url,
        tenant_id=scope[1],
        environment=scope[0],
        timeout=context.settings.kb_service_timeout_seconds,
    )
    try:
        rows = [r for r in client.list_knowledge_bases() if r["knowledge_base_id"] == scope[3]]
    except KnowledgeSearchUnavailable as exc:
        raise CandidateKnowledgeError(
            502, "DIFY_KNOWLEDGE_UNAVAILABLE", "knowledge retrieval temporarily unavailable"
        ) from exc
    if len(rows) != 1:
        raise CandidateKnowledgeError(404, "DIFY_KNOWLEDGE_NOT_FOUND", "knowledge source not found")
    return client, rows[0]


def catalogue(context, scope):
    active(context.settings, scope)
    _, row = _owned(context, scope)
    active(context.settings, scope)
    return {
        "knowledge_items": [
            {"knowledge_id": knowledge_id(scope), "name": row["name"], "knowledge_base_ids": [scope[3]]}
        ],
        "total": 1,
    }


def retrieve(context, scope, request):
    from aiops_diagnostics.dify.dify_knowledge_api import dify_records
    from aiops_diagnostics.knowledge.knowledge_retrieval import (
        KnowledgeSearchUnavailable,
        normalize_search_response,
    )

    if request.knowledge_id != knowledge_id(scope):
        raise CandidateKnowledgeError(404, "DIFY_KNOWLEDGE_NOT_FOUND", "knowledge source not found")
    active(context.settings, scope)
    client, _ = _owned(context, scope)
    signer = context.runtime.media_signer
    if signer is None:
        raise CandidateKnowledgeError(
            502, "DIFY_KNOWLEDGE_UNAVAILABLE", "knowledge retrieval temporarily unavailable"
        )
    top_k = request.top_k or context.settings.kb_search_max_results
    try:
        raw = client.search((scope[3],), request.query, top_k)
    except KnowledgeSearchUnavailable as exc:
        raise CandidateKnowledgeError(
            502, "DIFY_KNOWLEDGE_UNAVAILABLE", "knowledge retrieval temporarily unavailable"
        ) from exc
    active(context.settings, scope)
    chunks = normalize_search_response(
        raw,
        tenant_id=namespace(scope),
        agent_version=MEDIA_PREFIX + knowledge_id(scope),
        session_id=None,
        knowledge_base_ids=(scope[3],),
        media_signer=signer,
        max_results=top_k,
    )
    return {
        "records": dify_records(
            chunks, score_threshold=request.score_threshold, public_base_url=context.settings.public_base_url
        )
    }
