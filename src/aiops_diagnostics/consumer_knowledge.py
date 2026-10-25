"""WF01 知识只从本轮召回且已审核的条目交付；模型选择条目，不改业务事实。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.chat_execution import ChatRunUnavailable
from aiops_diagnostics.knowledge_retrieval import KnowledgeSearchGuard
from aiops_diagnostics.private_files import validate_private_file

SAFETY = re.compile(
    r"冒烟|火花|烧焦|触电|裸线|导线裸露|设备浸水|异常(?:发热|过热)|"
    r"smok(?:e|ing)|sparks?|burning smell|electric shock|exposed wires|submerged",
    re.I,
)
HUMAN = re.compile(
    r"转人工|人工客服|真人客服|联系人工|human (?:agent|support)|speak to (?:a person|someone)", re.I
)
MESSAGES = {
    "zh": {
        "safety": (
            "请立即远离设备，不触碰充电枪、线缆或外壳，不自行拔枪或维修。"
            "在确保自身安全的情况下提醒周围人员远离，并联系现场工作人员和当地紧急服务。"
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
            "Move away from the charger. Do not touch the connector, cable or enclosu"
            "re, unplug it or attempt repairs. If safe, warn others to keep clear and"
            " contact on-site staff and local emergency services."
        ),
        "human": (
            "Use the official support option in this app or on the station signage. D"
            "o not share passwords, one-time codes or full payment card details."
        ),
        "not_found": (
            "I could not find reliable instructions for this situation. Please share "
            "the charger message or the step you reached, or contact official support"
            "."
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
            "This live information is not connected to this support flow yet, so I ca"
            "nnot verify it. Please check the order or payment history in the app, or"
            " contact official support."
        ),
    },
}


def text_for(code, language):
    return MESSAGES["zh" if language.startswith("zh") else "en"][code]


def load_content(path: str, *, tenant: str, environment: str) -> dict:
    p = Path(path)
    validate_private_file(p)
    if p.stat().st_size > 2_000_000:
        raise ValueError("consumer content too large")
    body = json.loads(p.read_text())
    if not isinstance(body, dict):
        raise ValueError("invalid consumer content")
    if (
        body.get("schema") != "aiops-consumer-reviewed/1"
        or body.get("state") != "published"
        or body.get("tenant_id") != tenant
        or body.get("environment") != environment
    ):
        raise ValueError("consumer content is not published for this scope")
    entries = body.get("entries")
    if not isinstance(entries, dict) or not entries:
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
        for language in ("zh", "en"):
            answer = row["answers"].get(language)
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000:
                raise ValueError("invalid consumer answer")
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
    from dataclasses import asdict

    return hashlib.sha256(
        (json.dumps(asdict(binding), sort_keys=True) + digest(binding.content_file)).encode()
    ).hexdigest()


def _require_binding(runtime, caller, binding):
    from aiops_diagnostics import chat_execution

    if (
        not runtime.gateway_settings.consumer_chatflow_enabled
        or chat_execution.select_binding(
            runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
        )
        != binding
    ):
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403)


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
    support = (content or {}).get("support", {}).get("zh" if language.startswith("zh") else "en")
    if support:
        text += "\n" + support["label"] + "：" + support["url"]
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


def select_context(runtime, caller, binding, query, language, qa_id):
    content = load_content(
        binding.content_file, tenant=caller.effective_tenant_id, environment=binding.environment
    )
    if runtime.kb_search_client is None or runtime.media_signer is None or not binding.knowledge_base_ids:
        return {"status": "unavailable", "candidates": []}
    guard = KnowledgeSearchGuard(
        runtime.kb_search_client.for_tenant(caller.effective_tenant_id),
        tenant_id=caller.effective_tenant_id,
        agent_version=f"{binding.agent_id}#v{binding.agent_version_no}",
        knowledge_base_ids=binding.knowledge_base_ids,
        media_signer=runtime.media_signer,
        session_id=qa_id,
        max_results=5,
        max_calls=1,
    )
    result = guard.search(query)
    candidates = {}
    for chunk in result.chunks:
        title_id = re.fullmatch(r"(KB-C-\d{3})(?:\.md)?", chunk.title or "")
        ids = [title_id[1]] if title_id else []
        for key in ids:
            row = content["entries"].get(key)
            if row is None or key in candidates:
                continue
            candidates[key] = {
                "faq_id": key,
                "title": row.get("title", ""),
                "answer": row["answers"]["zh" if language.startswith("zh") else "en"],
                "reference_id": chunk.reference_id,
                "media": [],
            }
    status = "found" if candidates else "not_found" if result.status.value == "found" else result.status.value
    return {"status": status, "candidates": list(candidates.values())[:5]}


def final_answer(raw, context, language, content):
    try:
        selected = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid structured answer") from exc
    if not isinstance(selected, dict) or set(selected) != {"kind", "faq_id"}:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid answer fields")
    kind = selected["kind"]
    if kind == "knowledge":
        row = next(
            (r for r in (context or {}).get("candidates", []) if r["faq_id"] == selected["faq_id"]), None
        )
        if row is None:
            raise ChatRunUnavailable("CHATFLOW_REFERENCE_INVALID", "answer was not retrieved this turn")
        return {
            "blocks": [
                {"kind": "text", "text": row["answer"]},
                {"kind": "reference", "reference_id": row["reference_id"], "title": row["title"]},
            ],
            "retrieval_status": "found",
        }
    if selected["faq_id"] != "" or kind not in {"human", "clarify", "realtime", "not_found", "unavailable"}:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "unsupported answer kind")
    if kind in {"not_found", "unavailable"}:
        if context is None:
            raise ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "knowledge call was not made")
        kind = "unavailable" if context["status"] in {"unavailable", "limited"} else "not_found"
    answer = fallback(kind, language, content)
    if kind == "unavailable":
        answer["retrieval_status"] = "unavailable"
    return answer


def callback_context(runtime, token):
    from aiops_diagnostics import chat_data_grant, chat_execution
    from aiops_diagnostics.agent_runs import scope_from_json
    from aiops_diagnostics.dify_run_store import still_valid

    grants = chat_data_grant.DataGrantStore(runtime.store.path)
    grant = chat_data_grant.usable(grants.by_token(token))
    run = runtime.dify_run_store.by_qa(grant.qa_id)
    if (
        run is None
        or run.kind != "service"
        or run.grant_id != grant.grant_id
        or run.order_no
        or (run.scope_fingerprint, run.tenant_id, run.environment, run.workflow_id)
        != (grant.scope_fingerprint, grant.tenant_id, grant.environment, grant.workflow_id)
    ):
        raise ChatRunUnavailable("GRANT_SCOPE_MISMATCH", "knowledge grant refused", http_status=403)
    caller = scope_from_json(run.scope_json)
    binding = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if binding is None or not binding.content_file or _binding_identity(binding) != run.binding_digest:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403)
    _require_binding(runtime, caller, binding)

    def valid():
        chat_data_grant.usable(grants.by_id(grant.grant_id))
        latest = runtime.dify_run_store.get(run.run_id)
        qa = runtime.store.get_assistant_question(run.qa_id, caller.scope_fingerprint)
        if (
            latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or qa is None
            or qa["status"] != "running"
        ):
            raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "knowledge request ended", http_status=403)
        _require_binding(runtime, caller, binding)
        if _binding_identity(binding) != run.binding_digest:
            raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge content changed", http_status=403)

    valid()
    cached = runtime.dify_run_store.context(run.run_id)
    if cached is not None:
        return cached
    if not grants.consume(grant.grant_id):
        raise ChatRunUnavailable("GRANT_ALREADY_CONSUMED", "knowledge request already used", http_status=409)
    qa = runtime.store.get_assistant_question(run.qa_id, caller.scope_fingerprint)
    # 本次输入来自已鉴权的QA，不接受回调重新指定问题或语言。
    result = select_context(runtime, caller, binding, qa["question"], run.language, run.qa_id)
    valid()
    if not runtime.dify_run_store.store_context(run.run_id, result):
        raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "knowledge result not committed", http_status=403)
    return result
