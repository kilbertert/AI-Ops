"""WF01 知识只从本轮召回且已审核的条目交付；模型选择条目，不改业务事实。

历史 ``callback_version`` 1–5 专属的回调接口 ``callback_context`` 与旧 FAQ 映射
已收拢至 :mod:`aiops_diagnostics.consumer.legacy_callbacks`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.common.i18n import localized_pack
from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.consumer.presentation import COMPOSED_LANGUAGES, COPY
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    KnowledgeSearchGuard,
)

_LOGGER = logging.getLogger("aiops.consumer")

SAFETY = re.compile(
    r"冒烟|火花|烧焦|触电|裸线|导线裸露|设备浸水|异常(?:发热|过热)|"
    r"smok(?:e|ing)|sparks?|burning smell|electric shock|exposed wires|submerged",
    re.I,
)
HUMAN = re.compile(
    r"转人工|人工客服|真人客服|联系人工|human (?:agent|support)|speak to (?:a person|someone)", re.I
)
# shortcut: 仅识别明确未解决反馈；扩展隐含表达前须增加业务标注验收。
UNRESOLVED = re.compile(
    r"^(?:还是不行|还是没用|仍然不行|仍未解决|还没解决|没有解决|问题依旧|"
    r"still not working|still doesn't work|still does not work|not resolved|didn't help)"
    r"(?:[\s，,。.!！?？]|$)",
    re.I,
)


MESSAGES = {
    "zh": {
        "safety": (
            "请立即远离设备，不触碰充电枪、线缆或外壳，不自行拔枪或维修。"
            "在确保自身安全的情况下提醒周围人员远离，并联系现场工作人员和当地紧急服务。"
        ),
        "photo_unreadable": (
            "图片中的提示不清晰。请重新拍摄清楚的错误提示，或直接输入页面上的文字；"
            "不要上传账号、支付凭据或个人信息。"
        ),
        "followup": (
            "请告诉我您进行到了哪一步，以及App或充电桩显示的错误提示。"
            "请勿反复启动、强行拔枪，或发送密码、验证码和完整支付信息。"
        ),
        "human": "请使用当前App或站点告示中的官方客服入口。请勿提供密码、验证码或完整银行卡信息。",
        "not_found": "暂时没有找到可靠的适用说明。请补充设备提示或具体遇到的步骤；仍未解决时请联系官方客服。",
        "unavailable": "目前暂时无法读取适用的操作说明，请稍后重试或联系官方客服。",
        "clarify": "请说明您需要了解的操作，或提供页面上的错误提示；不要发送密码、验证码或完整支付信息。",
        "realtime": (
            "这项实时信息目前尚未接入本客服流程，暂时无法核实。"
            "请在当前App的订单或支付记录中查看，或联系官方客服。"
        ),
    },
    "en": {
        "safety": (
            "Move away from the charger. Do not touch the connector, cable or enclosure, "
            "unplug it or attempt repairs. If safe, warn others to keep clear and"
            " contact on-site staff and local emergency services."
        ),
        "photo_unreadable": (
            "The message in the image is unclear. Please take a clearer photo or type "
            "the message. Do not upload account, payment or personal information."
        ),
        "followup": (
            "Which step did you reach, and what message is shown in the app or on the charger? "
            "Do not repeatedly start charging, force the plug out, or share passwords, "
            "one-time codes or full payment details."
        ),
        "human": (
            "Use the official support option in this app or on the station signage. "
            "Do not share passwords, one-time codes or full payment card details."
        ),
        "not_found": (
            "I could not find reliable instructions for this situation. Please share "
            "the charger message or the step you reached, or contact official support."
        ),
        "unavailable": (
            "The relevant instructions are temporarily unavailable. Please try later "
            "or contact official support."
        ),
        "clarify": (
            "Which charging step do you need help with, or what message is shown? Do "
            "not share passwords, one-time codes or full payment details."
        ),
        "realtime": (
            "This live information is not connected to this support flow yet, so I cannot "
            "verify it. Please check the order or payment history in the app, or"
            " contact official support."
        ),
    },
}


def text_for(code, language):
    if language in COPY:
        return COPY[language]["knowledge_" + code]
    return localized_pack(MESSAGES, language)[code]


def load_content(path: str, *, tenant: str, environment: str) -> dict:
    p = Path(path)
    validate_private_file(p)
    if p.stat().st_size > 2_000_000:
        raise ValueError("consumer content too large")
    return validate_content(json.loads(p.read_text()), tenant=tenant, environment=environment)


def binding_content(binding, caller):
    if binding.content_snapshot is not None:
        return validate_content(
            binding.content_snapshot, tenant=caller.effective_tenant_id, environment=binding.environment
        )
    return load_content(
        binding.content_file, tenant=caller.effective_tenant_id, environment=binding.environment
    )


def validate_content(body, *, tenant, environment):
    from aiops_diagnostics.consumer.consumer_documents import SCHEMA

    if not isinstance(body, dict):
        raise ValueError("invalid consumer content")
    if (
        body.get("schema") not in {"aiops-consumer-reviewed/1", SCHEMA}
        or body.get("state") != "published"
        or body.get("tenant_id") != tenant
        or body.get("environment") != environment
    ):
        raise ValueError("consumer content is not published for this scope")
    documents = body.get("schema") == SCHEMA
    entries = body.get("entries", {})
    if not isinstance(entries, dict) or (not entries and not documents) or (documents and entries):
        raise ValueError("consumer content has no entries")
    for key, row in entries.items():
        if (
            not re.fullmatch(r"KB-C-\d{3}", key)
            or not isinstance(row, dict)
            or row.get("state") != "approved"
        ):
            raise ValueError("consumer FAQ is not approved")
        if not isinstance(row.get("answers"), dict):
            raise ValueError("invalid consumer answers")
        for language in COMPOSED_LANGUAGES:
            answer = row["answers"].get(language)
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000:
                raise ValueError("invalid consumer answer")
    media = body.get("media", {})
    if not isinstance(media, dict) or len(media) > 100 or (documents and media):
        raise ValueError("invalid consumer media registry")
    for key, item in media.items():
        if not isinstance(item, dict) or item.get("state") != "approved":
            raise ValueError("consumer media is not approved")
        for field in ("knowledge_base_id", "document_id", "backend_id", "title"):
            if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > 256:
                raise ValueError("invalid consumer media identity")
        if item.get("language", "any") not in {"any", *COMPOSED_LANGUAGES}:
            raise ValueError("invalid consumer media language")
        allowed = {"image": ALLOWED_IMAGE_TYPES, "video": ALLOWED_VIDEO_TYPES}.get(item.get("kind"), ())
        if item.get("mime_type") not in allowed or not re.fullmatch(r"[A-Z]+-\d{3}", key):
            raise ValueError("invalid consumer media type")
    for row in entries.values():
        ids = row.get("media_ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 4
            or any(not isinstance(i, str) or i not in media for i in ids)
        ):
            raise ValueError("invalid FAQ media references")
    if not isinstance(body.get("support", {}), dict):
        raise ValueError("invalid support configuration")
    for support in body.get("support", {}).values():
        if not isinstance(support, dict) or set(support) != {"label", "url"}:
            raise ValueError("invalid support route")
        url = support["url"]
        if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
            raise ValueError("invalid support URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "tel"}
            or (parsed.scheme == "https" and not parsed.netloc)
            or (parsed.scheme == "tel" and not re.fullmatch(r"\+?[0-9-]{3,24}", parsed.path))
            or parsed.username
            or parsed.password
        ):
            raise ValueError("invalid support URL")
        if not isinstance(support["label"], str) or not support["label"].strip():
            raise ValueError("missing support label")
    return body


def _binding_identity(binding):
    """指纹的唯一实现在 `consumer_binding.identity`（回读/取数/worker 共用）。"""
    from aiops_diagnostics.consumer.consumer_binding import identity

    return identity(binding)


def digest(path):
    if not path:
        return ""
    p = Path(path)
    validate_private_file(p)
    if p.stat().st_size > 2_000_000:
        raise ValueError("consumer content too large")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fallback(code, language, content=None):
    text = text_for(code, language)
    support = _support_line(language, content)
    if support:
        text += "\n" + support
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


#: 这三类答复没有任何本轮证据，所以正文可以由提示词写，但一个数字都不许出现。
NOTICE_KINDS = frozenset({"clarify", "not_found", "human"})


def _support_line(language, content):
    """受控的客服入口行。它由内容包给，不由模型写。"""
    routes = (content or {}).get("support", {})
    support = routes.get(language) or routes.get("en") or routes.get("zh")
    if not support:
        return ""
    label = support["label"] + ": " if routes.get(language) is support else ""
    return label + support["url"]


def notice(kind, reply, language, content):
    """提示词为澄清／无结果／转人工写的正文。客服入口行仍由系统追加。"""
    from aiops_diagnostics.common.answer_language import AnswerSurface, answer_chinese_leak
    from aiops_diagnostics.consumer.consumer_facts import validate_notice

    text = validate_notice(reply, language=language)
    support = _support_line(language, content)
    if support:
        text += "\n" + support
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": text}], retrieved_titles=[])
    if answer_chinese_leak(surface, language):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_LANGUAGE_INVALID", "notice language differs")
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


def followup_context(runtime, caller, question, conversation_id, *, require_result, before_run_id=None):
    state = {"topic": question, "unresolved_count": 0}
    if not conversation_id or not UNRESOLVED.search(question.strip()):
        return state
    previous = runtime.dify_run_store.latest_conversation_run(
        conversation_id, caller.scope_fingerprint, before_run_id=before_run_id
    )
    if previous is None:
        return state
    qa = runtime.store.get_assistant_question(previous.qa_id, caller.scope_fingerprint)
    if qa is None or qa["status"] != "completed":
        raise ChatRunUnavailable(
            "CHATFLOW_NEW_CONVERSATION_REQUIRED", "start a new conversation", http_status=409
        )
    require_result(runtime, caller, qa)
    prior = (runtime.dify_run_store.context(previous.run_id) or {}).get("followup", {})
    return {
        "topic": prior.get("topic", qa["question"]),
        "unresolved_count": min(prior.get("unresolved_count", 0) + 1, 2),
    }


def owned_libraries(runtime, tenant, app_id=""):
    """读取租户在当前环境可见且带分发授权标记的知识库目录。"""
    from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants, audience
    from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable

    try:
        if getattr(getattr(runtime, "gateway_settings", None), "knowledge_delegation_file", ""):
            return for_application(runtime, tenant, app_id).list_knowledge_bases()
        if runtime.kb_search_client is None:
            raise KnowledgeSearchUnavailable("catalogue unavailable")
        rows = runtime.kb_search_client.for_tenant(tenant).list_knowledge_bases()
    except KnowledgeSearchUnavailable as exc:
        raise ValueError("知识库目录暂不可用，请稍后重试") from exc
    settings = runtime.gateway_settings
    granted = KnowledgeGrants(settings.database_file).granted(tenant, settings.execution_environment)
    return audience(rows, granted)


def require_available(runtime, caller, binding):
    """校验当前发布绑定所选知识库仍归属租户且允许对客分发。"""
    from aiops_diagnostics.consumer.knowledge_access import require_owned

    if binding.callback_version < 3:
        return
    try:
        require_owned(
            binding.knowledge_base_ids,
            owned_libraries(runtime, caller.effective_tenant_id, binding.app_id),
        )
    except ValueError as exc:
        raise ChatRunUnavailable(
            "KNOWLEDGE_SELECTION_UNAVAILABLE", "所选知识库不可用，请重新选择并发布"
        ) from exc


def workspace_libraries(runtime, tenant, workspace):
    """目录能力只能读取本环境已登记应用的授权库，不扩为管理凭据。"""
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    rows = [
        row
        for row in SelfServiceStore(runtime.gateway_settings.database_file).apps(entry="consumer")
        if (row["workspace_id"], row["tenant_id"], row["environment"])
        == (workspace, tenant, runtime.gateway_settings.environment)
    ]
    if len(rows) != 1:
        raise ValueError("workspace application is ambiguous")
    return owned_libraries(runtime, tenant, rows[0]["app_id"])


def select_context(runtime, caller, binding, query, language, qa_id):
    """检索现役动态文档块上下文（v4+）；历史 v1–3 FAQ 检索见 ``legacy_callbacks``。"""
    from aiops_diagnostics.consumer.consumer_documents import context_from_chunks
    from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application
    from aiops_diagnostics.knowledge.knowledge_retrieval import (
        KnowledgeSearchResult,
        KnowledgeSearchUnavailable,
        RetrievalStatus,
    )

    binding_content(binding, caller)
    if getattr(getattr(runtime, "gateway_settings", None), "knowledge_delegation_file", ""):
        if runtime.media_signer is None:
            return {"status": "unavailable", "candidates": []}
        try:
            delegated = for_application(runtime, caller.effective_tenant_id, binding.app_id)
            result = delegated.search(
                binding.knowledge_base_ids,
                query,
                5,
                media_signer=runtime.media_signer,
                agent_version=f"consumer:{binding.agent_id}#v{binding.agent_version_no}",
                session_id=qa_id,
            )
        except KnowledgeSearchUnavailable:
            result = KnowledgeSearchResult(RetrievalStatus.UNAVAILABLE, (), 1)
        return context_from_chunks(result)
    if runtime.kb_search_client is None or runtime.media_signer is None or not binding.knowledge_base_ids:
        return {"status": "unavailable", "candidates": []}
    guard = KnowledgeSearchGuard(
        runtime.kb_search_client.for_tenant(caller.effective_tenant_id),
        tenant_id=caller.effective_tenant_id,
        agent_version=f"consumer:{binding.agent_id}#v{binding.agent_version_no}",
        knowledge_base_ids=binding.knowledge_base_ids,
        media_signer=runtime.media_signer,
        session_id=qa_id,
        max_results=5,
        max_calls=1,
    )
    return context_from_chunks(guard.search(query))


def live_answer(snapshot, language, *, localized=True, pack=None):
    if snapshot.get("source") == "cpms.orderDetail":
        from aiops_diagnostics.consumer.billing import billing_answer

        return billing_answer(snapshot, language, localized=localized, pack=pack)
    from aiops_diagnostics.consumer.presentation import status_answer

    return status_answer(snapshot, language, localized=localized, pack=pack)
