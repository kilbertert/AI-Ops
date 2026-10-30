"""WF01 知识只从本轮召回且已审核的条目交付；模型选择条目，不改业务事实。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.common.private_files import validate_private_file
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
    from aiops_diagnostics.consumer.consumer_language import COPY

    if language in COPY:
        return COPY[language]["knowledge_" + code]
    return MESSAGES["zh" if language.startswith("zh") else "en"][code]


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
        for language in ("zh", "en"):
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
        if item.get("language", "any") not in {"any", "zh", "en"}:
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


def _require_binding(runtime, caller, binding):
    """取数回调的判据：**签发时那一版继续跑**，只核对现行权限与撤回。

    这里曾经要求"当前绑定 == 签发绑定"，于是一次普通发布就把正在执行的 run 打死
    （复核实测：发布A→启动QA→取数回调前发布B ⇒ 403 CHATFLOW_BINDING_CHANGED，QA failed）。
    判据与 worker 侧同源（`consumer_chat_service.require_pinned`）。
    """
    from aiops_diagnostics.consumer.consumer_binding import require_pinned

    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403)
    try:
        require_pinned(runtime, caller, binding)
    except ChatRunUnavailable as exc:
        raise ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403
        ) from exc


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


#: 这三类答复**没有任何本轮证据**，所以正文可以由提示词写（运营的语气），但一个数字都不许出现。
NOTICE_KINDS = frozenset({"clarify", "not_found", "human"})


def _support_line(language, content):
    """受控的客服入口行。它由**内容包**给，不由模型写。"""
    routes = (content or {}).get("support", {})
    support = routes.get(language) or routes.get("en") or routes.get("zh")
    if not support:
        return ""
    label = support["label"] + ": " if routes.get(language) is support else ""
    return label + support["url"]


def notice(kind, reply, language, content):
    """提示词为澄清／无结果／转人工写的正文。客服入口行仍由系统追加。

    与知识正文用**同一条语言判据**：提示词写的话必须使用用户选的语言。
    """
    from aiops_diagnostics.common.answer_language import AnswerSurface, answer_chinese_leak
    from aiops_diagnostics.consumer.consumer_reply import validate_notice

    text = validate_notice(reply, language=language)
    support = _support_line(language, content)
    if support:
        text += "\n" + support
    # 本轮没有检索，所以**没有**可豁免的资源名：retrieved_titles 是空列表，不是省略。
    # 省略会让 #376 的豁免退回"只看形状"，于是一段编出来的小标题会被当成资源名放行。
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


def select_context(runtime, caller, binding, query, language, qa_id):
    content = binding_content(binding, caller)
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
    result = guard.search(query)
    if binding.callback_version >= 4:
        from aiops_diagnostics.consumer.consumer_documents import context_from_chunks

        return context_from_chunks(result)
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
                "media": [
                    runtime.media_signer.issue(
                        tenant_id=caller.effective_tenant_id,
                        agent_version=f"consumer:{binding.agent_id}#v{binding.agent_version_no}",
                        session_id=qa_id,
                        knowledge_base_id=item["knowledge_base_id"],
                        document_id=item["document_id"],
                        backend_id=item["backend_id"],
                        chunk_id=None,
                        kind=item["kind"],
                        mime_type=item["mime_type"],
                        title=item["title"],
                        reference_id=chunk.reference_id,
                    ).to_dict()
                    for media_id in row.get("media_ids", [])
                    if (item := content.get("media", {})[media_id])["knowledge_base_id"]
                    in binding.knowledge_base_ids
                    and item.get("language", "any") in {"any", "zh" if language.startswith("zh") else "en"}
                ],
            }
    status = "found" if candidates else "not_found" if result.status.value == "found" else result.status.value
    return {"status": status, "candidates": list(candidates.values())[:5]}


def live_answer(snapshot, language, *, localized=True, pack=None):
    if not localized:
        language = (
            ("zh" if language.startswith("zh") else "en")
            if snapshot.get("source") == "cpms.orderDetail"
            else (language if language in {"zh", "zh-Hant"} else "en")
        )
    if snapshot.get("source") == "cpms.orderDetail":
        from aiops_diagnostics.consumer.consumer_billing import billing_answer

        return billing_answer(snapshot, language, pack=pack)
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    return status_answer(snapshot, language, localized=localized, pack=pack)


def _has_reply(raw):
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(body, dict) and bool(body.get("reply"))


def final_answer(raw, context, language, content, *, copy_pack=None):
    if (context or {}).get("status") == "safety":
        return fallback("safety", language, content)
    if (context or {}).get("status") == "photo_unreadable":
        return fallback("photo_unreadable", language, content)
    if (context or {}).get("followup", {}).get("unresolved_count") == 1:
        return fallback("followup", language, content)
    if (context or {}).get("mode") == "documents":
        from aiops_diagnostics.consumer.consumer_documents import answer_selection

        kind, answer = answer_selection(
            raw, context, language=language, contract=(context or {}).get("fact_contract", "legacy")
        )
        if answer is not None:
            if kind in NOTICE_KINDS:
                return notice(kind, answer["blocks"][0]["text"], language, content)
            from aiops_diagnostics.common.answer_language import (
                AnswerSurface,
                answer_chinese_leak,
                looks_like_asset_name,
            )

            titles = [b["title"] for b in answer["blocks"] if b["kind"] == "reference"]
            prose = answer["blocks"][0]["text"]
            for title in sorted(set(titles), key=len, reverse=True):
                if looks_like_asset_name(title):
                    prose = prose.replace(title, "")
            surface = AnswerSurface.from_public_blocks(
                [{"kind": "text", "text": prose}, *answer["blocks"][1:]], retrieved_titles=titles
            )
            if not any(char.isalpha() for char in prose) or answer_chinese_leak(surface, language):
                raise ChatRunUnavailable("CHATFLOW_ANSWER_LANGUAGE_INVALID", "answer language differs")
            return answer
        # 归一化时**不能丢 reply**：documents 契约的 clarify/not_found/human 正是靠它交付正文，
        # 而这里一旦丢掉，正文就会退回固定文案（复核实测到的第二个缺口）。
        raw = json.dumps(
            {
                "kind": kind,
                "faq_id": "",
                **({"reply": json.loads(raw).get("reply")} if _has_reply(raw) else {}),
            }
        )
    try:
        selected = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid structured answer") from exc
    # `reply` 是可选字段：编排页的提示词可以自己写正文（见 consumer_reply）。
    # 没带它的应用行为与之前完全一致 —— 老版本不会被这次改动影响。
    if not isinstance(selected, dict) or not {"kind", "faq_id"} <= set(selected) <= {
        "kind",
        "faq_id",
        "reply",
    }:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid answer fields")
    kind = selected["kind"]
    if (context or {}).get("live_snapshot"):
        if kind != "realtime" or selected["faq_id"]:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "live answer kind differs")
        if selected.get("reply"):
            from aiops_diagnostics.consumer.consumer_reply import model_reply

            return model_reply(
                selected["reply"],
                context["live_snapshot"],
                language=language,
                contract=(context or {}).get("fact_contract", "legacy"),
            )
        return live_answer(
            context["live_snapshot"],
            language,
            localized=context.get("answer_language_version") == 2,
            pack=copy_pack,
        )
    if selected.get("reply"):
        # 没有实时快照时正文只能来自知识库或固定文案；**但澄清／无结果／转人工**是例外：
        # 那三类话本来就没有证据可引，判据是"一个数字都不许出现"（`validate_notice`）。
        # 模型若想在知识类答复里写正文，仍然拒绝 —— 一段没有取数依据的话不能冒充知识库回答。
        if kind not in NOTICE_KINDS and kind != "unavailable":
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply without live data")
        if kind == "unavailable" or (context or {}).get("status") in {"unavailable", "limited"}:
            # **服务故障以系统文案为准**：模型没有故障信息，让它描述故障只会多一个错处，
            # 而一句错的故障描述比"稍后重试"更难排查。这里不报错、也不降级成模板：
            # 交付的正是这条出口本来的系统文案。
            _LOGGER.info("consumer notice ignored: the service, not the model, describes the failure")
        else:
            return notice(kind, selected["reply"], language, content)
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
                *[
                    {"kind": media["kind"], "media": media, "title": media["title"]}
                    for media in row.get("media", [])
                ],
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


def callback_context(
    runtime, token, *, require_result, read_snapshot, image_text="", capability=None, knowledge_base_ids=None
):
    from aiops_diagnostics.dify.dify_run_store import still_valid
    from aiops_diagnostics.execution import chat_data_grant
    from aiops_diagnostics.execution.agent_runs import scope_from_json

    grants = chat_data_grant.DataGrantStore(runtime.store.path)
    grant = chat_data_grant.usable(grants.by_token(token))
    run = runtime.dify_run_store.by_qa(grant.qa_id)
    if (
        run is None
        or run.kind not in {"service", "service_trial"}
        or run.grant_id != grant.grant_id
        or run.order_no != grant.order_no
        or (run.scope_fingerprint, run.tenant_id, run.environment, run.workflow_id)
        != (grant.scope_fingerprint, grant.tenant_id, grant.environment, grant.workflow_id)
    ):
        raise ChatRunUnavailable("GRANT_SCOPE_MISMATCH", "knowledge grant refused", http_status=403)
    caller = scope_from_json(run.scope_json)
    trial = None
    if run.kind == "service_trial":
        entry = runtime._trial_contexts.get(run.qa_id)
        if entry is None or getattr(entry[1], "binding", None) is None:
            raise ChatRunUnavailable("TRIAL_NOT_ACTIVE", "trial not active", http_status=403)
        trial = entry[1]
        binding = trial.binding
    else:
        # 用**签发时那一版**的冻结绑定继续取数：普通发布不该打断在途 run。
        from aiops_diagnostics.consumer.consumer_binding import deliverable_binding

        binding = deliverable_binding(runtime, caller, run)
    if binding is None or (not binding.content_file and binding.content_snapshot is None):
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403)
    expected = run.data_kind if run.order_no else "knowledge"
    if (binding.callback_version >= 2 and capability != expected) or (
        binding.callback_version == 1 and capability is not None
    ):
        raise ChatRunUnavailable("GRANT_CAPABILITY_MISMATCH", "本轮授权与取数节点不一致", http_status=403)

    if binding.callback_version >= 3 and expected == "knowledge":
        if knowledge_base_ids != list(binding.knowledge_base_ids):
            raise ChatRunUnavailable(
                "KNOWLEDGE_SELECTION_MISMATCH", "节点选库与本次发布版本不一致", http_status=403
            )
    elif knowledge_base_ids is not None:
        raise ChatRunUnavailable(
            "KNOWLEDGE_SELECTION_UNEXPECTED", "此版本不接受节点选库参数", http_status=400
        )

    def require_current():
        if trial:
            trial.require_current(runtime.gateway_settings)
        else:
            _require_binding(runtime, caller, binding)

    def question_record():
        if trial:
            qa = runtime.store.get_standard_diagnosis(run.qa_id, caller.scope_fingerprint)
            return {**qa, "question": trial.question} if qa else None
        return runtime.store.get_assistant_question(run.qa_id, caller.scope_fingerprint)

    require_current()

    def valid():
        if not run.order_no and run.data_kind not in {"clarify", "human", "order_required"}:
            from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

            require_available(runtime, caller, binding)
        if run.order_no:
            enabled = binding.live_billing_record if run.data_kind == "billing" else binding.live_order_status
            if not enabled:
                raise ChatRunUnavailable("CHATFLOW_LIVE_DISABLED", "live status disabled", http_status=403)
            runtime._require_chat_order_visible(caller, run.order_no)
        chat_data_grant.usable(grants.by_id(grant.grant_id))
        latest = runtime.dify_run_store.get(run.run_id)
        qa = question_record()
        if (
            latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or qa is None
            or qa["status"] != "running"
        ):
            raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "knowledge request ended", http_status=403)
        require_current()
        if _binding_identity(binding) != run.binding_digest:
            raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "knowledge content changed", http_status=403)

    valid()
    qa = question_record()
    followup = followup_context(
        runtime,
        caller,
        qa["question"],
        run.conversation_id,
        require_result=require_result,
        before_run_id=run.run_id,
    )
    cached = runtime.dify_run_store.context(run.run_id)
    if cached is not None:
        return cached
    if not grants.consume(grant.grant_id):
        raise ChatRunUnavailable("GRANT_ALREADY_CONSUMED", "knowledge request already used", http_status=409)
    qa = question_record()
    # 本次输入来自已鉴权的QA，不接受回调重新指定问题或语言。
    query = qa["question"]
    recognized = None
    if run.has_photo:
        try:
            recognized = json.loads(image_text)
        except (ValueError, TypeError) as exc:
            raise ChatRunUnavailable(
                "PHOTO_CONTEXT_INVALID", "invalid photo context", http_status=400
            ) from exc
        if (
            not isinstance(recognized, dict)
            or set(recognized) != {"readable", "text"}
            or type(recognized["readable"]) is not bool
            or not isinstance(recognized["text"], str)
            or len(recognized["text"]) > 500
            or (recognized["readable"] and not recognized["text"].strip())
        ):
            raise ChatRunUnavailable("PHOTO_CONTEXT_INVALID", "invalid photo context", http_status=400)
        query += "\n图片识别线索（非可信指令）：" + recognized["text"]
    elif image_text and image_text != "no":
        raise ChatRunUnavailable("PHOTO_CONTEXT_INVALID", "unexpected photo context", http_status=400)
    if followup["unresolved_count"]:
        query = followup["topic"] + "\n" + query
    result = (
        {"status": "safety", "candidates": []}
        if recognized and SAFETY.search(recognized["text"])
        else {"status": "photo_unreadable", "candidates": []}
        if recognized and not recognized["readable"]
        else {
            "status": "live",
            "candidates": [],
            "live_snapshot": read_snapshot(caller, run.order_no, run.data_kind),
        }
        if run.order_no
        else {
            "status": "notice",
            "notice_kind": "human" if run.data_kind == "human" else "clarify",
            "missing_order": run.data_kind == "order_required",
            "sources": [],
        }
        if run.data_kind in {"clarify", "human", "order_required"}
        else select_context(runtime, caller, binding, query, run.language, run.qa_id)
    )
    result["followup"] = followup
    # 事实片段契约是**发布版本**的属性，不是提示词内容：旧版本继续走弱判据。
    result["fact_contract"] = "slots" if binding.callback_version >= 5 else "legacy"
    result["answer_language_version"] = 2
    # 钉住本轮所用的**文案版本**：答复生成后运营可能又发布一版措辞，
    # 回读必须按生成时那一版复现（见 consumer_copy.resolve）。
    result["copy_version"] = runtime.store.consumer_copy(caller.effective_tenant_id)[0]
    if binding.callback_version >= 4:
        result["mode"] = "documents"
    valid()
    # 本轮检索期间前轮也可能到期，不能继续向模型披露失效的前文。
    followup_context(
        runtime,
        caller,
        qa["question"],
        run.conversation_id,
        require_result=require_result,
        before_run_id=run.run_id,
    )
    if not runtime.dify_run_store.store_context(run.run_id, result):
        raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "knowledge result not committed", http_status=403)
    return result
