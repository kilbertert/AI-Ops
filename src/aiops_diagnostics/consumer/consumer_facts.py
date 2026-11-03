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
_A_NOT_A = re.compile(r"(?:有没有|是不是|能不能|会不会|可不可以|要不要|对不对|行不行|有没有什么|是否)")


def _state_term_hit(clause: str, terms: tuple[str, ...]) -> bool:
    """子句里是否有**状态**词——名词化的场所/设备词先剥掉，剩下的命中才算状态。"""
    stripped = clause
    for noun in _STATE_NOUNS:
        stripped = stripped.replace(noun, "　")
    return any(term in stripped for term in terms)


#: 否定词与状态词之间允许出现的状语。它们不改变「否定的是状态」这个判断：
#: `还没结束`、`一直没充电完成`、`始终未完结` 都必须仍然算改写状态。
_NEGATION_ADVERBS = r"(?:仍|还|再|也|都|就|已|曾经|完全|根本|一直|始终|暂时|尚|并)*"


def _negates_state(clause: str, terms: tuple[str, ...]) -> bool:
    """这个子句是不是在**否定订单状态**——否定词必须落在状态词的前面。

    ## 为什么不能只判「同子句共现」

    2026-11-03 实测：41 上同一张单（`2116050516255272961`）72 次运行里 53 次失败，
    其中 13 次的日志是 `unsupported_fact detail=status line=3`。按同一份 manifest
    重放 10 次模型调用，4 次被这条判据拦下，拦的都是这种句子：

    * `我这边暂未取到本次**充电**的过程样本` —— 否定的对象是样例，不是订单状态；
    * `本次没有取到**充电**过程明细`；
    * `我无法仅凭现有记录判断费用是否**异常**` —— 否定的是"能判断"，不是"异常"。

    共现判据把「未 + 充电」当成状态改写，于是一条**如实说明缺证**的句子被判成编造状态。
    这正是提示词明确要求写的内容（「监测0条必须明确缺证」），两端规格互相打架：
    提示词要求写缺证，判据一看到缺证里的「未/没有/无法 + 充电/异常」就整轮拒绝。
    用户看到的就是 `<operation> 失败：OPERATOR_DIAGNOSIS_FAILED`，且**时好时坏** ——
    模型每次措辞不同，撞上的概率约四成。

    ## 判据

    否定词后面（允许状语）**紧跟**状态词才算改写：`订单还没结束`、`未充电完成`。
    `未取到…过程样本` 里「未」后面是 `取到`，不是状态词，放行。
    """
    stripped = clause
    for noun in _STATE_NOUNS:
        stripped = stripped.replace(noun, "　")
    for negation in _NEGATIONS:
        for match in re.finditer(re.escape(negation) + _NEGATION_ADVERBS + r"(?=.)", stripped):
            tail = stripped[match.end() :]
            if any(tail.startswith(term) for term in terms):
                return True
    return False


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


#: 结算状态词。**我们没有任何接口能给出它**（见 ADR-0193），所以旁白里**断言**它必然是
#: 凭空断言。但「提到」不等于「断言」：`请先核对账单。`、`目前没有结算状态的证据。`
#: 恰恰是我们**要求模型写**的两类内容（核对建议、缺证说明）。
#:
#: 所以这里判的是**断言形式**，不是词是否出现（2026-11-03 修：第一版整段出现即拒，
#: 把上面两类正常句子全拒了，八条正例全红）。
_SETTLEMENT_TERMS = ("结算", "对账", "對賬", "settle", "reconcil")

#: 结算状态**被陈述**的形式。共同点：必须有**谓语/完成体**，光有名词不算。
#:
#: 第一版把「结算 + 任意字符回溯到『状态』」当成断言，于是 `请核对结算状态。` 被拒 ——
#: 那是名词，不是陈述（2026-11-03 评审实测）。所以第 ③ 组现在要求状态词后面**真的
#: 接了谓语**（是/为/：），或者那句话以状态名词收尾但前面有主语断言。
_SETTLEMENT_ASSERTION = re.compile(
    # ① 结算 + 完成/进行体：结算了 / 结算完了 / 结算完成 / 结算中 / 结算给运营商
    r"(?:结算|对账|對賬|settle|reconcil)\s*"
    r"(?:了|完了|完成|中|完毕|给|至|到|completed|done|pending|in\s+progress)"
    # ② 已/未/尚未/仍在… + 结算（肯定与否定同等对待）
    r"|(?:已|已经|尚未|还未|未|没有|还没|正在|仍在|still)\s*(?:结算|对账|對賬|settle|reconcil)"
    # ③ 结算状态 + 谓语 + **内容**：`结算状态是已结算` 是断言；`结算状态是…？`
    #    剥掉疑问词后剩「结算状态是」，谓语后面没有内容，不是断言。
    #    `结算状态如何？` 连谓语都没有，也不匹配。
    r"|(?:结算|对账|對賬)\s*状态\s*(?:是|为|：|:|is)\s*[^\s　，,。；;！!？?]",
    re.IGNORECASE,
)

#: 疑问标记。分句切分把标点吃掉了，所以判问句要看**切分前**的原文。
_QUESTION_MARK = re.compile(r"[？?]|(?:是否|有没有|要不要|需不需要|能否|可否|如何|怎么样)")

#: **否定有证据**的说法：它们提到结算，但陈述的是「没有证据」，必须放行。
#: 与上面的断言形式一起用 —— 先剥掉这些，再判断言。
_NO_EVIDENCE_ABOUT_SETTLEMENT = re.compile(
    r"(?:没有|无|未|未提供|不包含|不在|缺|缺少|无法|不可用|尚未取得)"
    r"[^。；;！!？?，,\n]{0,10}(?:结算|对账|對賬)"
    r"[^。；;！!？?，,\n]{0,10}(?:证据|字段|数据|信息|记录|状态|来源|内|范围)"
    r"|(?:结算|对账|對賬)[^。；;！!？?，,\n]{0,10}(?:没有|无)证据"
    r"|(?:结算|对账|對賬)[^状态。；;！!？?，,\n]{0,6}状态[^。；;！!？?，,\n]{0,6}"
    r"(?:不在|不包含|没有|无)",
    re.IGNORECASE,
)


def _settlement_refused() -> None:
    """结算断言：拒。一个薄封装，避免同一条长 message 重复三次。"""
    raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "settlement status has no evidence source")


def _settlement_asserted(clause: str) -> bool:
    """这个子句是否在**陈述**结算状态（而不是提到、询问或说明没有证据）。"""
    stripped = _NO_EVIDENCE_ABOUT_SETTLEMENT.sub("　", clause)
    return bool(_SETTLEMENT_ASSERTION.search(stripped))


def check_status_prose(text: str) -> None:
    """状态只由事实段给出：旁白不能断言、也不能**否定**状态。

    三个维度，都是有限的：
    1. **否定词 × 状态词（同子句）** —— `尚未完结，仍在供电`、`has not finished` 都在这里；
    2. **断言形式** —— `正在/仍在/还在/已经 + 状态动词`、`is/still + charging`；
    3. **结算状态** —— 我们**没有**这个字段的证据，所以**断言**即拒绝（不做肯定/否定之分：
       没证据时「还没结算」与「已结算」是同等强度的断言）。但**提到**结算不拒 ——
       核对建议与缺证说明本来就要提它。

    刻意**不**拦截裸状态词：`Do you have any other questions about charging?` 是正常追问，
    把它当断言拒绝正是上一版的错误（见 tests/consumer/test_prompt_fact_segments.py 的 B 例）。
    """
    terms = _state_terms()
    low_all = text.casefold()
    # 结算：判**断言形式**，不是词出现。
    #
    # 问句豁免**不能在分句之后判**：切分把 `？` 吃掉了，`是否已结算？` 会被当成陈述；
    # 反过来，按 `_A_NOT_A` 整句跳过又会把「这单已结算是否需要帮助？」里的断言一起放掉
    # （2026-11-03 评审两个方向都实测到）。所以先按整句问句判定，再逐分句找断言。
    for sentence in re.split(r"[。；;！!\n\r]", text):
        if not sentence.strip():
            continue
        if _QUESTION_MARK.search(sentence):
            # 问句豁免分两种情况，**必须先区分再剥**：
            #   ① 整句就是一个问句（`是否已结算？`、`结算状态是什么？`）—— 没有任何陈述，
            #      直接放行。剥掉疑问词再判会把 `已结算`、`结算了` 当成断言（误拒）。
            #   ② 陈述 + 问句（`这单已结算是否需要帮助？`）—— 剥掉疑问部分后，
            #      剩下的陈述仍要判。
            # head 是问句**之前**的部分。`这单结算了吗？` 的 head 是 `这单结算了`，
            # 但那句的「结算了」本身就在疑问之内（`了吗` 是问句形态），所以 head
            # 末尾的完成体要先摘掉再判。
            head = re.split(
                r"是否|有没有|要不要|需不需要|能否|可否|如何|怎么样|什么|吗|呢|吧",
                sentence,
            )[0]
            head = re.sub(r"(?:结算|对账|對賬)\s*(?:了|完了)$", "", head) or head
            if _settlement_asserted(head):
                _settlement_refused()
            continue
        if _settlement_asserted(sentence):
            _settlement_refused()
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
        if _negates_state(low, terms):
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
