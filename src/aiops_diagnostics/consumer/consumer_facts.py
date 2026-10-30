"""提示词写表达，系统写事实：**带含义的事实片段由系统填值**。

判据的形态在 2026-10-30 的独立复核后改过一次，原因写在下面，因为它是本模块的全部理由。

## 为什么不再用"数字比对 + 关键词正则"

第一版判据是：金额"该有的都在"、每个数字都能在证据里找到、不含内部标识，再加几条正则
（金额标签绑定、货币单位、到账承诺、状态方向）。复核用 10 条反例证明它**两个方向都错**：

- **放行错话**：`20是合计，27.36是付款`（值在标签前）、`finished 说成"仍在充电中"`、
  `我帮你找到了虚构乙站`、`The matching station is free and available.`
- **拒绝正确的话**：`合计27.36元，付款20元，退款0元`（货币单位被当成"编造的数字"）、
  `结束后可查看账单`、`我已完成本次查询`（"结束/完成"被当成状态断言）。

根因是它在**猜自然语言的意思**：正则能列举的词永远少于模型能写的句子。所以判据换成一个
可穷举的形状：

1. **事实只能是片段**。模型写 ``合计{{fact:total_amount}}``，系统用本轮快照填值。
   值、单位、状态名、订单号、时间全部由系统给 —— 事实正确**由构造保证**，不靠校验。
2. **模型自己的文字里不许出现数字**（槽位之外）。钱数、数量、时长、单号都走片段，
   于是"编一个 3 元/小时"没有位置可写。
3. **状态名与支付结果这两组词是封闭词表**，只能在片段里出现。它们是**有限集合**
   （公司状态表的 5 个标签 + 支付结果的几个词），不是"语义理解"。
4. 其余措辞完全自由：`结束后可查看账单`、`我已完成本次查询`、`别着急` 都通过。

## 老版本怎么办

已发布的旧应用（`callback_version < 5`）的提示词里没有片段，行为与改动前一致：走
:func:`check_legacy` 的三条弱判据。**旧版本继续可用**是明确要求，代价写在这里：旧契约拦不住
上面那四类错话，所以迁移到片段契约是一个**显式动作**（见 `consumer_documents.PROTOCOL_BLOCK`
与 `tools/content/upgrade_consumer_flow.py`）。
"""

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


def _status_label(snapshot: dict, language: str) -> str:
    """状态名走**现成的多语言资源**，不是"非中文一律英文"。"""
    from aiops_diagnostics.consumer.consumer_plain import _status_name

    return _status_name(language, snapshot)


def _readable_time(raw: object) -> str:
    from aiops_diagnostics.consumer.consumer_time import readable_time

    return readable_time(raw)


def facts_for(snapshot: dict, language: str) -> dict[str, str]:
    """本轮**可以**填进正文的**整句**事实。不在这个字典里的名字一律拒收。

    每一段都自带标签、值与来源，所以"把这个值说成那个意思"没有位置可写：模型拿到的是
    `账单合计 27.36 元，系统记录的支付金额 20 元，退款 0 元`，不是一个 `27.36`。
    """
    facts: dict[str, str] = {}
    order_no = str(snapshot.get("order_no") or "")
    if order_no:
        facts["order_line"] = ("订单号：" if language.startswith("zh") else "Order: ") + order_no
    amounts = {
        name: _amount(snapshot.get(field))
        for name, field in (
            ("total", "order_amount"),
            ("paid", "recorded_payment_amount"),
            ("refund", "recorded_refund_amount"),
        )
    }
    if any(value is not None for value in amounts.values()):
        facts["billing_line"] = _billing_line(amounts, language)
    if snapshot.get("status_role"):
        # `status` 只用于自检/文档（多语言状态名），**不是**可以写进正文的片段：
        # 裸状态名能被摆到"并非…"这种位置（复核反例），所以正文只允许 `status_line`。
        facts["status"] = _status_label(snapshot, language)
        facts["status_line"] = _status_line(snapshot, language)
    if snapshot.get("observed_at"):
        facts["observed_line"] = (
            "查询时间：" if language.startswith("zh") else "Checked at: "
        ) + _readable_time(snapshot["observed_at"])
    return facts


def _unit(language: str) -> str:
    return "元" if language.startswith("zh") else "CNY"


def _billing_line(amounts: dict, language: str) -> str:
    """一句完整的话：三笔金额各自带标签与单位，顺序由系统定。"""
    unit = _unit(language)
    zh = language.startswith("zh")
    parts = []
    if amounts["total"] is not None:
        parts.append(("账单合计 " if zh else "Total ") + f"{amounts['total']} {unit}")
    if amounts["paid"] is not None:
        parts.append(("系统记录的支付金额 " if zh else "Recorded payment ") + f"{amounts['paid']} {unit}")
    if amounts["refund"] is not None:
        parts.append(("退款 " if zh else "Refund ") + f"{amounts['refund']} {unit}")
    return ("，".join(parts) if zh else "; ".join(parts)) + ("。" if zh else ".")


def _status_line(snapshot: dict, language: str) -> str:
    label = _status_label(snapshot, language)
    return ("当前状态：" if language.startswith("zh") else "Status: ") + label


def status_words() -> tuple[str, ...]:
    """公司状态表里的**全部**标签，加英文角色词。这是封闭集合，不是语义猜测。"""
    from aiops_diagnostics.data.order_status import StatusRole
    from aiops_diagnostics.health.rules import ORDER_STATUS

    labels = [label for label in ORDER_STATUS.values() if label]
    roles = [role.replace("_", " ") for role in StatusRole if role != StatusRole.UNKNOWN]
    return tuple(labels) + tuple(roles)


def labels_for_role(role: str) -> set[str]:
    """这个角色在状态表里对应的**全部**标签（中文）与角色词（英文）。

    用角色而不是只用状态码：快照里 `status_code` 可能缺失（回放、合成数据），而
    `status_role` 一定在 —— 只按状态码取标签会把它判成"未知"，于是**正确**的答复被拒。
    """
    from aiops_diagnostics.data.order_status import role_of
    from aiops_diagnostics.health.rules import ORDER_STATUS

    labels = {label for code, label in ORDER_STATUS.items() if role_of(code).value == role}
    labels.add(role.replace("_", " "))
    return {label for label in labels if label}


def _own_text(text: str) -> str:
    """去掉片段后的"模型自己写的字"。数字与封闭词表只在这里检查。"""
    return SLOT.sub(" ", text)


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[max(0, start - 12) : start]))


def fill(text: str, facts: dict[str, str]) -> str:
    """把片段填成值。未知片段是协议错误，不是措辞问题。"""
    allowed = {name for name in CLIENT_FACTS if name in facts}
    unknown = sorted({name for name in SLOT.findall(text) if name not in allowed})
    if unknown:
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_UNKNOWN", f"reply uses facts this turn does not carry: {', '.join(unknown)}"
        )

    def replace(match: re.Match[str]) -> str:
        return facts[match.group(1)]

    return SLOT.sub(replace, text)


def _label_groups() -> dict[str, tuple[str, ...]]:
    """金额标签 → 它指的字段。**封闭词表**：读者会把哪些词读成哪个金额，是可以列举的。"""
    return {
        "order_amount": ("账单合计", "订单合计", "合计", "总额", "总金额", "订单金额", "应付", "total"),
        "recorded_payment_amount": ("支付", "付款", "实付", "已付", "paid", "payment"),
        "recorded_refund_amount": ("退款", "已退", "退回", "refund"),
    }


#: 金额为 0 时，**"没有这一项"就是它的说法**：`没有退款记录` 已经说完了
#: `recorded_refund_amount=0` 这个事实，再要求写出 `0` 只会逼模型说一句不自然的话。
ZERO_SAID = {
    "recorded_refund_amount": re.compile(
        r"(?:没有|没|无|未|no|not?)\s*(?:任何)?\s*(?:退款|退回|refund)", re.I
    ),
}

#: 标签后面紧跟这些词时，它说的是状态码而不是金额（`退款状态 1`）。
_NOT_AMOUNT_TAIL = ("状态", "status", "标记", "码", "记录", "说明", "政策", "规则", "流程", "入口")
_CLAUSE_BREAK = "。；;！!？?，,\n\r"
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_CURRENCY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|人民币|¥|￥|RMB|CNY|yuan|美元|USD|\$|€|EUR)", re.IGNORECASE
)


def _token_numbers(text: str, order_no: str) -> list[tuple[Decimal, int, int]]:
    """正文里的数字（先挖掉订单号，再扫）。订单号不是金额，也不是计数。"""
    masked = text.replace(order_no, "\0" * len(order_no)) if order_no else text
    found = []
    for match in _NUMBER_TOKEN.finditer(masked):
        try:
            found.append((Decimal(match.group(0)), match.start(), match.end()))
        except InvalidOperation:  # pragma: no cover
            continue
    return found


def _nearest(numbers, text: str, start: int, end: int):
    """标签**两侧**最近的数字，不跨子句。

    只看一侧会漏掉 `20是合计，27.36是付款` —— 值写在标签前面也是同一句断言。
    """
    window = text[end : end + 12]
    cut = next((i for i, ch in enumerate(window) if ch in _CLAUSE_BREAK), len(window))
    after = [n for n, s, _ in numbers if end <= s <= end + cut]
    if after:
        return after[0]
    window = text[max(0, start - 12) : start]
    cut = max((i for i, ch in enumerate(window) if ch in _CLAUSE_BREAK), default=-1)
    before = [n for n, _, e in numbers if start - (len(window) - cut - 1) <= e <= start]
    return before[-1] if before else None


def check_bound_reply(text: str, snapshot: dict, language: str) -> str:
    """片段契约：事实可以写成片段，也可以直接写 —— 但必须与证据对得上。

    四条判据，全部建立在**封闭集合**上（金额标签、货币单位、公司状态表的标签、支付结果词）：

    1. 订单号必须出现（片段或原文）；
    2. 每个数字都要有出处；带货币单位的数字必须是快照里的某个金额；
    3. **金额必须挂在它自己的标签上**，值在标签前、标签后都算 —— `20是合计，27.36是付款`
       两个值都真、标签互换，这一条才拦得住；
    4. 状态名只能取自公司状态表，且必须与本轮快照一致 —— `finished` 说成"仍在充电中"被拦，
       而 `结束后可查看账单`、`我已完成本次查询` 这类**不含完整状态名**的措辞不受影响。

    另外支付结果（到账/退款成功/扣款）仍是封闭词表，否定形式放行。
    """
    facts = facts_for(snapshot, language)
    allowed = {name for name in CLIENT_FACTS if name in facts}
    unknown = sorted({name for name in SLOT.findall(text) if name not in allowed})
    if unknown:
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_UNKNOWN", f"reply uses facts this turn does not carry: {', '.join(unknown)}"
        )
    uses_fragments = bool(SLOT.search(text))
    filled = fill(text, facts)
    # 用了片段就必须**全部**用片段：混写直接数字等于让模型自己给值安排含义
    # （复核反例 `每小时{{fact:paid_amount}}元`）。片段之外一个数字都不许有。
    if uses_fragments and _NUMBER_TOKEN.search(SLOT.sub(" ", filled)):
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_UNWRAPPED_NUMBER",
            "reply mixes fact fragments with hand-written numbers",
        )
    order_no = str(snapshot.get("order_no") or "")
    if order_no and order_no not in filled:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply omits the order number")
    for token in ("cpms.", "status_code=", "status_role="):
        if token in filled:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply exposes internal identifiers")
    # 金额**直接从快照取**（facts 里是整句片段，不是数字）。
    amounts = {
        field: Decimal(value)
        for field in ("order_amount", "recorded_payment_amount", "recorded_refund_amount")
        if (value := _amount(snapshot.get(field))) is not None
    }
    numbers = _token_numbers(filled, order_no)
    present = {value for value, _, _ in numbers}
    # (2) 每个数字都要有出处；小计数（0–10 整数）不构成对钱数的断言。
    known = {Decimal(token) for token in _NUMBER_TOKEN.findall(_evidence(snapshot, language))}
    invented = {n for n in present if n not in known and not (n == n.to_integral() and 0 <= n <= 10)}
    if invented:
        raise ChatRunUnavailable(
            "CHATFLOW_ANSWER_INVALID", "reply contains numbers that are not in the evidence"
        )
    # (2b) 带货币单位的数字必须是快照里的金额：`每小时 3 元` 在这里被拦下。
    for match in _CURRENCY.finditer(filled):
        if Decimal(match.group(1)) not in set(amounts.values()):
            raise ChatRunUnavailable(
                "CHATFLOW_FACT_INVENTED_AMOUNT",
                "reply states a currency amount that is not in the evidence",
            )
    # (3) 金额标签绑定（两侧）。
    fields = {
        "order_amount": "order_amount",
        "recorded_payment_amount": "recorded_payment_amount",
        "recorded_refund_amount": "recorded_refund_amount",
    }
    values = {field: amounts.get(field) for field in fields}
    for field, labels in _label_groups().items():
        expected = values.get(field)
        for label in labels:
            for match in re.finditer(re.escape(label), filled, re.IGNORECASE):
                tail = filled[match.end() : match.end() + 4].lstrip("：: ")
                if any(tail.startswith(word) for word in _NOT_AMOUNT_TAIL):
                    continue
                found = _nearest(numbers, filled, match.start(), match.end())
                if found is None:
                    continue
                if expected is None or found != expected:
                    raise ChatRunUnavailable(
                        "CHATFLOW_FACT_LABEL_MISMATCH",
                        f"reply binds {label} to a value that is not the recorded {field}",
                    )
    # 金额不许漏写：快照里有的金额必须出现在正文里（片段或原文）。
    for field in fields:
        value = values.get(field)
        if value is not None and value not in present:
            if value == 0 and (pattern := ZERO_SAID.get(field)) and pattern.search(filled):
                continue
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", f"reply does not carry the recorded {field}")
    # (4) 状态名取自封闭表，且必须与本轮一致。
    role = str(snapshot.get("status_role") or "")
    if role and role != "unknown":
        recorded = labels_for_role(role)
        for label in status_words():
            if label and label in filled and label not in recorded:
                raise ChatRunUnavailable(
                    "CHATFLOW_FACT_STATUS_INVERTED",
                    f"reply states a status ({label}) that is not this order's",
                )
    for claim in PAYMENT_OUTCOME:
        for match in re.finditer(re.escape(claim), filled, re.IGNORECASE):
            if not _negated(filled, match.start()):
                raise ChatRunUnavailable(
                    "CHATFLOW_FACT_PROMISE",
                    "reply promises a payment outcome the business record does not carry",
                )
    return filled


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
