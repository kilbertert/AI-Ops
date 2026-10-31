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
#: 否定词。与状态词**同子句**出现时，这句话就在改写状态 —— 与用哪个同义词无关。
_NEGATIONS = ("未", "没有", "没", "尚未", "并未", "不", "并非", "无", "not", "never", "isn't", "hasn't")
#: 状态断言形式（不是词表）：`正在/仍在/还在/已经/已` + 状态动词，或英文 `is/still/has` + 状态词。
_ASSERTION = re.compile(
    r"(?:正在|仍在|还在|已经|已|尚未|未|仍|还)\s*"
    r"(?:充[电電]|供[电電]|[结結]束|完成|完结|進行|进行|停止|异常|異常)"
    r"|\b(?:is|are|was|were|still|has|have)\s+(?:charging|finished|completed|ended|ongoing|pending|stopped)\b",
    re.IGNORECASE,
)
#: 产品用来描述订单状态的名词/动词。**这是维护中的词汇，不是对自然语言的穷举**：
#: 它来自公司状态表、现役多语言文案（`consumer_language.COPY`）与运营/客服的实际说法。
#: 判据只有"否定词 × 这些词"与"断言形式"两维，所以换一个同义词不会绕过第一维。
_STATE_SYNONYMS = ("充电", "供电", "结束", "完成", "完结", "进行", "停止", "异常")

#: **名词化**的状态词：它们是场所或设备（`充电站`/`充电桩`/`充电枪`），不是状态断言。
#: 只有在命中这些复合词时才跳过该处匹配，单独的 `充电` 仍然算状态 —— 否则
#: `你是不是想核对订单对应的充电站` 这类正常追问会被误拒（2026-10-31 真实模型样本实测）。
_STATE_NOUNS = (
    "充电站",
    "充电桩",
    "充电枪",
    "充电线",
    "充电口",
    "充电卡",
    "充电记录",
    "异常单",
    "异常码",
)

#: A-不-A 疑问式（`有没有`/`是不是`/`能不能`/`会不会`）：含否定字，但整句不是在否定。
_A_NOT_A = re.compile(
    r"(?:有没有|是不是|能不能|会不会|可不可以|要不要|对不对|行不行|有没有什么|是否)"
)


def _state_term_hit(clause: str, terms: tuple[str, ...]) -> bool:
    """子句里是否有**状态**词——名词化的场所/设备词先剥掉，剩下的命中才算状态。"""
    stripped = clause
    for noun in _STATE_NOUNS:
        stripped = stripped.replace(noun, "　")
    return any(term in stripped for term in terms)


def _state_terms() -> tuple[str, ...]:
    from aiops_diagnostics.consumer.consumer_language import COPY
    from aiops_diagnostics.health.rules import ORDER_STATUS

    terms = set(_STATE_SYNONYMS)
    terms.update(label for label in ORDER_STATUS.values() if label)
    for table in COPY.values():
        for key in ("charging", "finished", "waiting", "starting", "stopping", "abnormal"):
            value = table.get(key)
            if isinstance(value, str) and value:
                terms.add(value.casefold())
    return tuple(sorted(terms, key=len, reverse=True))


def _state_labels() -> tuple[str, ...]:
    """现役多语言文案里的状态词（`consumer_language.COPY`）。这是产品资源，不是同义词表。"""
    from aiops_diagnostics.consumer.consumer_language import COPY

    labels = set()
    for table in COPY.values():
        for key in ("charging", "finished", "waiting", "starting", "stopping", "abnormal"):
            value = table.get(key)
            if isinstance(value, str) and len(value) >= 3:
                labels.add(value.casefold())
    return tuple(sorted(labels, key=len, reverse=True))


def check_status_prose(text: str) -> None:
    """状态只由事实段给出：旁白不能断言、也不能**否定**状态。

    两个维度，都是有限的：
    1. **否定词 × 状态词（同子句）** —— `尚未完结，仍在供电`、`has not finished` 都在这里；
    2. **断言形式** —— `正在/仍在/还在/已经 + 状态动词`、`is/still + charging`。

    刻意**不**拦截裸状态词：`Do you have any other questions about charging?` 是正常追问，
    把它当断言拒绝正是上一版的错误（见 tests/consumer/test_prompt_fact_segments.py 的 B 例）。
    """
    terms = _state_terms()
    low_all = text.casefold()
    # 现役文案里的**状态标签**本身就是状态断言：出现在旁白里就是复述状态（含否定复述）。
    # 词形按词干匹配（`terminé`/`terminée`、`abgeschlossen`/`abgeschlossene`）。
    if any(label in low_all or (len(label) >= 5 and label[:-1] in low_all) for label in _state_labels()):
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "status belongs in the evidence segment")
    for clause in re.split(r"[。；;！!？?，,\n\r]", text):
        low = clause.casefold()
        if not low.strip():
            continue
        # `有没有异常` / `是不是想核对` 是 A-不-A 疑问式：含否定字却不否定任何事。
        # 按子句剥掉问句里的 A-不-A，再判否定 —— 否则正常追问会被当成状态改写。
        if _A_NOT_A.search(low):
            low = _A_NOT_A.sub("", low)
        if any(mark in low for mark in _NEGATIONS) and _state_term_hit(low, terms):
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "status belongs in the evidence segment")
        if _ASSERTION.search(clause):
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "status belongs in the evidence segment")


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
            check_status_prose(line)
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
