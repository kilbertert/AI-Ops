"""新版用独立事实段交付本轮证据；旧版保留已有正文判据。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 事实片段的形状。名字是**受控词汇**：只能取自 :func:`facts_for` 的键。
SLOT = re.compile(r"\{\{fact:([a-z_]{1,32})\}\}")

#: 客户端本轮能填的事实。**每个片段是一整句带含义的话**（标签+值+单位+来源），
#: 不是裸值 —— 裸值可以被摆到任何位置上表达任何意思（复核反例：`每小时{{fact:paid_amount}}元`、
#: `合计{{fact:total_amount}}美元`）。片段由系统整句产出，模型只决定它出现在哪一段。
CLIENT_FACTS = ("order_line", "billing_line", "status_line", "observed_line")

#: 支付结果的**封闭词表**。系统只有业务记录，没有支付渠道回执，所以这些断言一律不成立。
#: 否定形式（`没有退款记录`、`not refunded`）放行 —— 那正是我们要说的话。
PAYMENT_OUTCOME = (
    "到账",
    "已经到账",
    "将到账",
    "已退款",
    "退款成功",
    "已扣款",
    "扣款成功",
    "原路退回",
    "refunded",
    "credited",
    "charged successfully",
)

#: 子句内的否定词：`不代表已经到账`、`没有退款记录` 不该被当成承诺。
_NEGATION = re.compile(
    r"(?:未|没有|没|尚未|并未|不|无|not|no|hasn'?t|has not|isn'?t)[^。；;！!？?，,\n\r]{0,6}$",
    re.IGNORECASE,
)
_CLAUSE = "。；;！!？?，,\n\r"
_NUMBER = re.compile(r"\d")


def _amount(raw: object) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return format(value, "f") if value.is_finite() else None


def _readable_time(raw: object) -> str:
    from aiops_diagnostics.consumer.consumer_time import readable_time

    return readable_time(raw)


def facts_for(snapshot: dict, language: str) -> dict[str, str]:
    """事实段复用现有本语种正文，包含订单、来源、时间及业务限制。"""
    from aiops_diagnostics.consumer.consumer_billing import billing_answer
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    facts = {}
    if snapshot.get("order_no"):
        facts["order_line"] = str(snapshot["order_no"])
    if snapshot.get("observed_at"):
        facts["observed_line"] = _readable_time(snapshot["observed_at"])
    if snapshot.get("status_role"):
        facts["status_line"] = status_answer(snapshot, language, localized=True)["blocks"][0]["text"]
    if any(
        snapshot.get(key) is not None
        for key in ("order_amount", "recorded_payment_amount", "recorded_refund_amount")
    ):
        facts["billing_line"] = billing_answer(snapshot, language)["blocks"][0]["text"]
    return facts


def render_segments(text: str, snapshot: dict, language: str) -> str:
    """模型只引用完整事实段；片段与自由表达不能拼成同一句断言。"""
    facts = facts_for(snapshot, language)
    references = SLOT.findall(text)
    required = "billing_line" if "billing_line" in facts else "status_line"
    if required not in references or any(key not in facts for key in references):
        raise ChatRunUnavailable("CHATFLOW_FACT_UNKNOWN", "required evidence segment missing")
    for line in text.splitlines():
        if SLOT.search(line):
            if SLOT.fullmatch(line.strip()) is None:
                raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "fact segment must be a separate line")
        elif line.strip():
            check_notice(line)
    own = SLOT.sub("", text)
    if "{{" in own or "}}" in own:
        raise ChatRunUnavailable("CHATFLOW_FACT_UNKNOWN", "unknown fact segment")
    return SLOT.sub(lambda match: facts[match.group(1)], text)


ZERO_SAID = {
    "recorded_refund_amount": re.compile(
        r"(?:没有|没|无|未|no|not?)\s*(?:任何)?\s*(?:退款|退回|refund)", re.I
    ),
}


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[max(0, start - 12) : start]))


def check_legacy(text: str, snapshot: dict, language: str) -> None:
    """旧契约（`callback_version < 5`）：三条弱判据，行为与改动前一致。

    **它拦不住**标签互换、编造的单价、状态说反 —— 那是它被替换的原因。保留它只为"旧发布版本
    继续可用"，并且判据强度写在这里，不让读者误以为老应用也受片段契约保护。
    """
    for token in ("cpms.", "status_code=", "status_role="):
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply exposes internal identifiers")
    order_no = str(snapshot.get("order_no") or "")
    if order_no and order_no not in text:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply omits the order number")
    # 旧契约原有的一条：快照里每个非空金额都必须在正文里（漏写会让读者以为那项不存在）。
    for field in ("order_amount", "recorded_payment_amount", "recorded_refund_amount"):
        raw = snapshot.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            expected = Decimal(str(raw))
        except InvalidOperation:
            continue
        if expected == 0 and ZERO_SAID["recorded_refund_amount"].search(text):
            continue
        if expected not in {Decimal(t) for t in re.findall(r"\d+(?:\.\d+)?", text)}:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", f"reply does not carry the recorded {field}")
    present = {Decimal(token) for token in re.findall(r"\d+(?:\.\d+)?", text)}
    known = {Decimal(token) for token in re.findall(r"\d+(?:\.\d+)?", _evidence(snapshot, language))}
    if present - known - {Decimal(n) for n in range(0, 11)}:
        raise ChatRunUnavailable(
            "CHATFLOW_ANSWER_INVALID", "reply contains numbers that are not in the evidence"
        )


def _evidence(snapshot: dict, language: str) -> str:
    import json

    return " ".join(
        [
            json.dumps(snapshot, ensure_ascii=False, default=str),
            _readable_time(snapshot.get("observed_at")),
        ]
    )


def check_notice(text: str) -> None:
    """澄清／无结果／转人工：本轮没有任何证据，所以**一个数字都不许出现**。

    链接也不许写：客服入口由内容包给、系统追加。支付结果与"已提交"类执行声明同样不许。
    """
    if _NUMBER.search(text):
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_UNWRAPPED_NUMBER", "notice states a number without any evidence this turn"
        )
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice must not write links")
    for claim in PAYMENT_OUTCOME:
        for match in re.finditer(re.escape(claim), text, re.IGNORECASE):
            if not _negated(text, match.start()):
                raise ChatRunUnavailable("CHATFLOW_FACT_PROMISE", "notice promises a payment outcome")
    for claim in ("已提交", "已为您提交", "已申请", "已转接", "已通知", "submitted", "transferred"):
        for match in re.finditer(re.escape(claim), text, re.IGNORECASE):
            if not _negated(text, match.start()):
                raise ChatRunUnavailable(
                    "CHATFLOW_FACT_PROMISE", "notice claims an action the system did not take"
                )
