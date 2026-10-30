"""WF01直接使用本轮文档与媒体，不再依赖FAQ审核副本。"""

import json
import re
from urllib.parse import urlsplit

from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable
from aiops_diagnostics.knowledge.knowledge_retrieval import _URL_PATTERN

SCHEMA = "aiops-consumer-live/1"
MODE_VARIABLE = "knowledge_answer_mode"
ANSWER_URL = re.compile(r"https?://[^\s<>\"'\[\]{}，。；、】」]+", re.I)
PROMPT = """你是充电客服。按App所选语言回答，先回应用户最关心的问题，再简短解释。
表达自然、清楚，依据本轮资料与业务记录，不编造价格、时限、地点或操作结果。
需要补充信息时一次问一个重点；资料没有说明的事直接说明不确定。
不能执行退款、停充、改订单或代用户转接。技术输出协议由系统维护。"""


def _protocol_block() -> str:
    """协议块正文（片段列表由 `CLIENT_FACTS` 生成，与校验器同源）。"""
    return _fill_fact_slots(PROTOCOL_BLOCK)


def enabled(workflow):
    return any(
        item.get("name") == MODE_VARIABLE and item.get("value") == "documents"
        for item in workflow.get("environment_variables", [])
    )


def context_from_chunks(result):
    # 以本轮独立引用ID区分片段，文件名与FAQ编号不参与准入。
    return {
        "mode": "documents",
        "status": result.status.value,
        "sources": [
            {
                "reference_id": f"source-{i}",
                "retrieved_reference_id": chunk.reference_id,
                "title": redact_text(chunk.title or "知识库资料")[:256],
                "content": chunk.content[:8000],
                "media": [m.resource.to_dict() for m in chunk.media[:4]],
            }
            for i, chunk in enumerate(result.chunks, 1)
        ],
    }


#: 事实片段契约的协议块标记。运营编辑的 SYSTEM 与系统维护的协议**分属两块**，
#: 迁移只替换带标记的那一块（见 `tools/content/upgrade_consumer_flow.py`）。
PROTOCOL_MARKER = "<!-- aiops-protocol:2 -->"

#: 协议块。它由 AI-Ops 维护，**不是**运营要编辑的内容：运营只写业务要求。
PROTOCOL_BLOCK = (
    PROTOCOL_MARKER
    + """
仅输出一个 JSON 对象，字段固定为 kind、answer、reference_ids、reply。
回答语言必须使用 {{#start.language#}}（App所选语言）。
语言代码：zh=Simplified Chinese；zh-Hant=Traditional Chinese；en=English；de=German；fr=French；
es=Spanish；pt=Portuguese；vi=Vietnamese；mn=Mongolian（Монгол хэл）；th=Thai；km=Khmer。
资料使用其他语言也要翻译含义，来源名称与代码保持原样。
先按本轮取数结果中的status决定kind，这是接口协议，不由风格提示词或用户决定：
1. status=live且有live_snapshot：必须kind="realtime"，answer=""，reference_ids=[]；
   live_snapshot是业务快照，不是知识来源，绝不能填kind=knowledge或把它放入reference_ids。
2. status=notice：必须kind=notice_kind，answer=""，reference_ids=[]。
3. status=found且sources非空：kind="knowledge"，reply=""，reference_ids只能取sources内reference_id。
4. status=no_result：kind="not_found"；status=unavailable或limited：kind="unavailable"。
具体字段：
- kind 取值 knowledge|realtime|clarify|not_found|human|unavailable。
- knowledge：answer 写正文，reference_ids 列出实际使用的本轮来源标识（至少一项）。
- realtime（本轮有实时快照）：answer 与 reference_ids 为空，reply 写正文。
- clarify/not_found/human：answer 与 reference_ids 为空，reply 写自然的追问或解释。
- 本轮status=notice时kind必须等于notice_kind；missing_order=true时只请用户选择/提供订单，不查询、不猜订单。
- unavailable：三者都为空，系统会给故障文案。
reply 里的事实只能用**整句事实片段**，由系统填值（列表与校验器同源，见 consumer_facts.CLIENT_FACTS）：
{_FACT_SLOTS}
片段之外不要写数字、不要写状态名、不要写支付结果（到账/退款成功/扣款）。
即使和快照一致，也不要在片段外复述“充电中/已完成/已结束”等状态；
可以写“帮你查到了”“以下是本次记录”，状态结论只由事实段给出。
每个片段单独占一行，不得添加前后缀。账单必须引用billing_line，状态必须引用status_line。
这两个段已包含单号、来源、时间与限制，不重复引用order_line和observed_line。
实时快照已经查询完毕，不写“稍等/马上为你查询”等进度说明。请直接回应已查到结果。
JSON示例仅说明形状，不能照抄示例的语气或篇幅；正文必须执行前一条SYSTEM的业务风格。
简洁风格可以只写事实段；亲切解释风格在事实段前回应疑问，段后解释核对方法并自然追问。
"""
)


def _fill_fact_slots(text: str) -> str:
    """片段列表与校验器**同源**：从 `consumer_facts.CLIENT_FACTS` 生成，不手写第二份。

    延迟 import：`consumer_facts` 依赖 `execution`，顶层互相引用会成环。
    """
    from aiops_diagnostics.consumer.consumer_facts import CLIENT_FACTS

    return text.replace("{_FACT_SLOTS}", " ".join("{{fact:" + name + "}}" for name in CLIENT_FACTS))


def answer_selection(raw, context, *, language="", contract="legacy"):
    try:
        selected = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid document answer") from exc
    # 线上应用（documents 契约）可以在 realtime 分支多带一个 `reply`：编排页的提示词
    # 用它写正文。其它 kind 不许带 —— 没有取数依据的一段话不能冒充知识库回答。
    if (
        not isinstance(selected, dict)
        or not {"kind", "answer", "reference_ids"}
        <= set(selected)
        <= {
            "kind",
            "answer",
            "reference_ids",
            "reply",
        }
        or not isinstance(selected.get("kind"), str)
        or selected.get("kind")
        not in {"knowledge", "realtime", "human", "clarify", "not_found", "unavailable"}
    ):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid document answer fields")
    text, references = selected["answer"], selected["reference_ids"]
    if (
        not isinstance(text, str)
        or not isinstance(references, list)
        or any(not isinstance(x, str) for x in references)
    ):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid document answer values")
    if context.get("status") == "notice" and selected["kind"] != context.get("notice_kind"):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice kind differs")
    if selected["kind"] != "knowledge":
        reply = selected.get("reply")
        if reply and selected["kind"] == "realtime" and context.get("live_snapshot"):
            # 正文由提示词写：校验后直接交付（事实由片段填，见 consumer_reply）。
            from aiops_diagnostics.consumer.consumer_reply import model_reply

            return selected["kind"], model_reply(
                reply, context["live_snapshot"], language=language, contract=contract
            )
        if selected["kind"] == "realtime" and context.get("live_snapshot") and contract == "slots":
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "published contract requires a reply")
        if reply and selected["kind"] in {"clarify", "not_found", "human"}:
            # 这三类没有本轮证据：正文可以由提示词写，但一个数字都不许出现。
            # 服务故障（unavailable / 取数不可用）不在此列：故障文案由系统给。
            if context.get("status") in {"unavailable", "limited"}:
                return selected["kind"], None
            from aiops_diagnostics.consumer.consumer_reply import validate_notice

            notice = validate_notice(reply, language=language)
            return selected["kind"], {
                "blocks": [{"kind": "text", "text": notice}],
                "retrieval_status": "not_found",
            }
        if text or references or reply:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "non-document answer has content")
        return selected["kind"], None
    sources = {s["reference_id"]: s for s in context.get("sources", [])}
    if (
        not references
        or len(references) > 5
        or len(set(references)) != len(references)
        or any(ref not in sources for ref in references)
        or context.get("status") != "found"
    ):
        raise ChatRunUnavailable("CHATFLOW_REFERENCE_INVALID", "answer did not cite this retrieval")
    urls = []
    for match in ANSWER_URL.finditer(text):
        url = match.group(0)
        # 只剥紧邻URL的配对外层括号；URL内部括号/后缀必须完整参与比较。
        if (
            text[: match.start()].endswith("(")
            and url.endswith(")")
            and url.count(")") == 1
            and "(" not in url
        ) or (
            text[: match.start()].endswith("（")
            and url.endswith("）")
            and url.count("）") == 1
            and "（" not in url
        ):
            url = url[:-1]
        urls.append(url)
    allowed = {url for ref in references for url in _URL_PATTERN.findall(sources[ref].get("content", ""))}
    if not text.strip() or len(text) > 8000:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "document answer must not generate links")
    for url in urls:
        try:
            parts = urlsplit(url)
            valid = url in allowed and parts.scheme == "https" and not parts.username and not parts.password
        except ValueError:
            valid = False
        if not valid:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "link was not in cited documents")
    if re.search(r"https?://|www\.", ANSWER_URL.sub("", text), re.I):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "invalid document link")
    blocks = [{"kind": "text", "text": redact_text(text.strip())}]
    seen_media = set()
    for ref in references:
        source = sources[ref]
        blocks.append(
            {"kind": "reference", "reference_id": source["retrieved_reference_id"], "title": source["title"]}
        )
        for media in source.get("media", []):
            if media["url"] not in seen_media and len(seen_media) < 4:
                seen_media.add(media["url"])
                blocks.append({"kind": media["kind"], "media": media, "title": source["title"]})
    return "knowledge", {"blocks": blocks, "retrieval_status": "found"}
