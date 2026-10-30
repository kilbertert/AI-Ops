"""节点选库、租户归属与只读目录凭据。目录凭据不能用于检索正文。"""

import base64
import hashlib
import hmac
import re

PROVIDER = "trendpower/consumer_knowledge/consumer_knowledge"
TOOL = "consumer_knowledge"


def catalogue_token(secret, environment, workspace):
    if not secret or not environment or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace):
        raise ValueError("catalogue credential configuration unavailable")
    encoded = base64.urlsafe_b64encode(workspace.encode()).decode().rstrip("=")
    digest = hmac.new(
        secret.encode(), f"consumer-kb-catalogue:1:{environment}:{workspace}".encode(), hashlib.sha256
    ).hexdigest()
    return f"ckc_{encoded}.{digest}"


def catalogue_workspace(token, secret, environment):
    try:
        encoded, _ = token.removeprefix("ckc_").split(".")
        workspace = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        expected = catalogue_token(secret, environment, workspace)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("catalogue credential refused") from exc
    if not hmac.compare_digest(expected.encode(), token.encode()):
        raise ValueError("catalogue credential refused")
    return workspace


def selected_libraries(node):
    config = node.get("tool_configurations", {}).get("knowledge_base_ids", {})
    if not isinstance(config, dict):
        raise ValueError("知识库选择必须是节点配置常量")
    values = config.get("value")
    if (
        config.get("type") != "constant"
        or not isinstance(values, list)
        or not 1 <= len(values) <= 20
        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("请选择一个或多个知识库；库列表必须是节点配置常量")
    return tuple(values)


AUDIENCE_CUSTOMER = "customer"


def require_owned(selected, rows):
    """选中的库必须**属于本租户**且**允许对客**。

    第二条不是"属于本租户"的推论：租户自己的内部资料库同样属于他，但不能交给消费者。
    判据是服务端登记（`consumer_knowledge_grants`），行上没有 `audience="customer"` 就是
    内部库 —— fail closed，缺字段一律按内部处理。
    """
    owned = {row["knowledge_base_id"] for row in rows}
    if not set(selected) <= owned:
        raise ValueError("所选知识库已删除或不属于当前租户，请重新选择")
    customer = {row["knowledge_base_id"] for row in rows if row.get("audience") == AUDIENCE_CUSTOMER}
    if not set(selected) <= customer:
        raise ValueError("所选知识库未授权给客户端使用（内部资料不能交给消费者），请重新选择")


def owned_libraries(runtime, tenant):
    from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants, audience
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable

    try:
        if runtime.kb_search_client is None:
            raise KnowledgeSearchUnavailable("catalogue unavailable")
        rows = runtime.kb_search_client.for_tenant(tenant).list_knowledge_bases()
    except KnowledgeSearchUnavailable as exc:
        raise ValueError("知识库目录暂不可用，请稍后重试") from exc
    settings = runtime.gateway_settings
    granted = KnowledgeGrants(settings.database_file).granted(tenant, settings.execution_environment)
    return audience(rows, granted)


def require_available(runtime, caller, binding):
    from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

    if binding.callback_version < 3:
        return
    try:
        require_owned(
            binding.knowledge_base_ids,
            owned_libraries(runtime, caller.effective_tenant_id),
        )
    except ValueError as exc:
        raise ChatRunUnavailable(
            "KNOWLEDGE_SELECTION_UNAVAILABLE", "所选知识库不可用，请重新选择并发布"
        ) from exc
