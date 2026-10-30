"""正文契约按发布版本选择；系统负责事实段，模型负责周围表达。"""

from __future__ import annotations

from aiops_diagnostics.consumer import consumer_facts
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 正文上限。答复是几句话，不是文档；放宽会让一次跑飞的模型把整屏刷满。
MAX_REPLY_CHARS = 2000

#: 澄清／无结果／转人工的上限。它们是一句话的通知。
MAX_NOTICE_CHARS = 600

#: 不许出现在用户正文里的内部标识。`cpms.` 是接口名，`status_code=` 是原始码。
FORBIDDEN = ("cpms.", "status_code=", "status_role=")


def validate_reply(reply: object, snapshot: dict, *, language: str, contract: str = "legacy") -> str:
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is empty")
    text = reply.strip()
    if len(text) > MAX_REPLY_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is too long")
    if contract == "slots":
        return consumer_facts.render_segments(text, snapshot, language)
    if contract != "legacy":
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "unknown answer contract")
    consumer_facts.check_legacy(text, snapshot, language)
    return text


def model_reply(reply: object, snapshot: dict, *, language: str, contract: str = "legacy") -> dict:
    """把模型写的正文包成 blocks-v1。前端只认 `blocks[]`，形状必须与既有答复一致。"""
    text = validate_reply(reply, snapshot, language=language, contract=contract)
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


def validate_notice(reply: object, *, language: str) -> str:
    """校验提示词为**澄清／无结果／转人工**写的正文。

    这三类答复没有任何本轮证据：没有快照、没有资料片段。所以判据是"一个数字都不许出现"，
    不许写链接，不许有支付结果或"已提交"类执行声明。没有带正文时行为与之前完全一致。
    """
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is empty")
    text = reply.strip()
    if len(text) > MAX_NOTICE_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is too long")
    for token in FORBIDDEN:
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice exposes internal identifiers")
    consumer_facts.check_notice(text)
    return text
