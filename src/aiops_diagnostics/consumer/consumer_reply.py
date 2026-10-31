"""正文契约按发布版本选择；系统负责事实段，模型负责周围表达。

两个契约并存，**由运行期按发布版本选择**：

* ``contract="slots"``（`callback_version >= 5`）：正文只能引用系统事实段，
  判据在 :func:`aiops_diagnostics.consumer.consumer_facts.render_segments`。
* ``contract="legacy"``（更早的发布版本）：逐条数字判据，保持**主线加固后**的强度 ——
  业务数值不能借取证时间、状态码或身份标识获得授权（见 :func:`_evidence_numbers`）。

拒收不是"降级成模板"而是**整轮失败**：一份混着假数字的答复比一句"服务暂时不可用"更糟。
"""

from __future__ import annotations

import re

from aiops_diagnostics.consumer import consumer_facts
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 正文上限。答复是几句话，不是文档；放宽会让一次跑飞的模型把整屏刷满。
MAX_REPLY_CHARS = 2000

#: 澄清／无结果／转人工的上限。它们是一句话的通知。
MAX_NOTICE_CHARS = 600

#: 不许出现在用户正文里的内部标识。`cpms.` 是接口名，`status_code=` 是原始码。
FORBIDDEN = ("cpms.", "status_code=", "status_role=")

#: 快照里"读者要能核对"的金额字段。空值不要求出现 —— 没给的值不能凭空写。
AMOUNT_FIELDS = ("order_amount", "recorded_payment_amount", "recorded_refund_amount")

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    found: set[float] = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(float(token))
        except ValueError:  # pragma: no cover - 正则保证可解析
            continue
    return found


def validate_reply(reply: object, snapshot: dict, *, language: str, contract: str = "legacy") -> str:
    """校验模型写的正文，返回可直接交付的文本。不合格就整轮失败。"""
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is empty")
    text = reply.strip()
    if len(text) > MAX_REPLY_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is too long")
    if contract == "slots":
        return consumer_facts.render_segments(text, snapshot, language)
    if contract != "legacy":
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "unknown answer contract")
    for token in FORBIDDEN:
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply exposes internal identifiers")
    order_no = str(snapshot.get("order_no") or "")
    if order_no and order_no not in text:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply omits the order number")
    # 金额：快照里给了的，正文里必须出现且数值相等。这是"不许漏写"的判据。
    from aiops_diagnostics.consumer.consumer_time import readable_time

    numeric_text = text
    # 只允许完整引用身份/取证时间，不能拿日期、秒数或订单号片段解释金额。
    for literal in (
        order_no,
        str(snapshot.get("observed_at") or ""),
        readable_time(snapshot.get("observed_at")),
    ):
        if literal and literal != "None":
            numeric_text = numeric_text.replace(literal, " ")
    present = _numbers(numeric_text)
    for field in AMOUNT_FIELDS:
        value = snapshot.get(field)
        if value is None or str(value).strip() == "":
            continue
        try:
            expected = float(str(value))
        except ValueError:
            continue
        if expected not in present:
            # 金额为 0 时，"没有这一项"就是它的说法：`没有退款记录` 已经说完了这个事实，
            # 再要求写出 `0` 只会逼模型说一句不自然的话。
            if expected == 0 and consumer_facts.ZERO_SAID["recorded_refund_amount"].search(text):
                continue
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", f"reply does not carry the recorded {field}")
    # 反过来：正文里的**每一个**数字都必须能在证据里找到出处。
    # 只要求"该有的都在"拦不住编造 —— 一段既有 2.40 又编了 2.5 的正文会照样通过。
    allowed = _evidence_numbers(snapshot, language)
    invented = sorted(present - allowed)
    if invented:
        raise ChatRunUnavailable(
            "CHATFLOW_ANSWER_INVALID", "reply contains numbers that are not in the evidence"
        )
    return text


def _evidence_numbers(snapshot: dict, language: str) -> set[float]:
    """业务数值不能借取证时间、状态码或身份标识获得授权。"""
    import json

    excluded = {"order_no", "observed_at", "source", "status_code", "status_role"}
    business = {key: value for key, value in snapshot.items() if key not in excluded}
    allowed = _numbers(json.dumps(business, ensure_ascii=False, default=str))
    allowed.update(float(n) for n in range(0, 11))
    return allowed


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
