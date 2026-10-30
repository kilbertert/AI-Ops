"""提示词写的正文里，**事实必须来自本轮快照**。

ADR-0187 给模型开了写正文的口子，判据当时只有三条：金额"该有的都在"、每个数字都能在证据里
找到、不含内部标识。三条都成立，仍然能写出三种错话 —— 它们都不是"数字不对"，而是**数字挂在
错的标签上**或**把没有的事说成有**：

* ``合计 20，付款 27.36``：两个值都是真的，标签互换了。数字集合比对发现不了。
* ``每小时 3 元``：``3`` 落在"0–10 的小计数"豁免里，于是凭空造出一个单价。
* ``已到账`` / ``未结束``：金额与状态都"在证据里"，断言的方向反了。

所以这一层不再问"数字对不对"，而是问"这句话把哪个值说成了什么"。四条判据，逐条对应上面
四种错话：

1. **金额必须挂在它自己的标签上**（:func:`check_amount_labels`）。``合计`` 旁边的数字必须等于
   快照的 ``order_amount``，``付款`` 旁边必须是 ``recorded_payment_amount``。标签没有对应
   字段时，它旁边的任何数字都是编的。
2. **货币断言只能引用快照里的金额**（:func:`currency_claims`）。``3 元`` 这种带单位的数字不再
   享受小计数豁免 —— 计数不构成对钱数的断言，价格构成。
3. **不承诺到账、退款、扣款**（:func:`check_promises`）。系统只有业务记录，没有支付渠道回执；
   "已到账"是一句我们无法证明的话。否定形式（"没有退款记录"）是允许的，它正是我们想说的。
4. **状态不能被说反**（:func:`check_status`）。快照说结束，正文不能说"未结束"；快照说充电中，
   正文不能说"已结束"。

**这一层能证明什么、不能证明什么**：它能拦住上面四类**可机械判定**的错话，不承诺证明所有诊断
推理都正确 —— 一句把因果关系说反、但每个值都挂在正确标签上的话仍会通过。那属于"业务准确率"，
只能由真实故障案例验收，不能由这里替代。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 快照里"读者要能核对"的金额字段。空值不要求出现 —— 没给的值不能凭空写。
AMOUNT_FIELDS = ("order_amount", "recorded_payment_amount", "recorded_refund_amount")

#: 金额标签 → 它指的字段。同义词按"读者会把它读成哪个金额"归组。
#: 不在表里的词不做绑定判断：这张表**只**声明会被误读的那些标签。
AMOUNT_LABELS: dict[str, tuple[str, ...]] = {
    "order_amount": (
        "账单合计",
        "订单合计",
        "合计",
        "总额",
        "总金额",
        "订单金额",
        "消费金额",
        "应付",
        "total",
        "order amount",
        "amount charged",
    ),
    "recorded_payment_amount": ("支付", "付款", "实付", "已付", "付了", "paid", "payment"),
    "recorded_refund_amount": ("退款", "已退", "退回", "refund"),
}

#: 标签后面紧跟这些词时，它说的不是金额而是状态码（``退款状态 1``）。
NOT_AMOUNT = ("状态", "status", "标记", "码", "记录")

#: 金额为 0 时，**“没有这一项”就是它的说法**。要求模型必须写出 ``0`` 反而逼它说一句
#: 不自然的话（“已退 0”），而读者要的事实是“没退过”。
ZERO_SAID = {
    "recorded_refund_amount": re.compile(
        r"(?:没有|没|无|未|no|not?)\s*(?:任何)?\s*(?:退款|退回|refund)", re.I
    ),
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
#: 数字 + 货币单位 = 对钱数的断言。``3 元`` 与 ``第 3 条`` 是两件事。
CURRENCY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|人民币|¥|￥|RMB|CNY|yuan|美元|USD|\$|€|EUR)",
    re.IGNORECASE,
)
#: 到账／退款／扣款的**肯定**断言。否定形式由 :func:`_negated` 放行。
PROMISE = re.compile(
    r"(?:"
    r"(?:已经|已|将|会|保证|立即|马上|今天|今日|预计|正在)\s*(?:到账|入账|原路退回|退回|退款|扣款)"
    r"|(?:到账|退款|退回|扣款)\s*(?:成功|已完成|完成|处理中|中)"
    r"|\b(?:has been|have been|will be|is being|guaranteed|promised)\s+"
    r"(?:refunded|credited|charged)\b"
    r"|\brefund(?:ed)?\s+successfully\b"
    r")",
    re.IGNORECASE,
)

#: 表示"这一单已经结束"的词。快照角色决定它们该出现还是该被否定。
FINISHED_WORDS = ("结束", "完成", "终止", "finished", "ended", "completed", "stopped")
#: 声称系统**已经做了什么**的话。诊断与客服面都不执行写操作，这类断言一律不成立。
EXECUTED = re.compile(
    r"(?:"
    r"(?:已经|已|成功|自动|完成|执行了?)\s*(?:为您|给你|帮您)?\s*"
    r"(?:提交|申请|退款|补发|重算|重发|建单|停充|停止充电|转接|转人工|通知)"
    r"|(?:提交|申请|退款|补发|重算|重发|建单|停充|转接)\s*(?:成功|已完成|完成|完毕)"
    r"|\b(?:has been|have been)\s+(?:submitted|refunded|recalculated|resent|created|stopped)\b"
    r")",
    re.IGNORECASE,
)
#: 子句边界。金额标签与数字跨过 ``。`` 或 ``,`` 就不再是同一句话：
#: ``退款 0。第 3 条是退款说明`` 里的 ``3`` 与「退款」无关。
_CLAUSE_BREAK = "。；;！!？?，,\n\r"
_NEGATION = re.compile(
    r"(?:未|没有|没|尚未|并未|不|not|hasn'?t|has not|isn'?t)[^。；;！!？?，,\n\r]{0,6}$", re.I
)

#: 标签后面看多远。**只看"标签在前"**这一种读法：中文与英文都把标签放在值前面
#: （``合计 27.36``、``paid 20``），而"值在前"的近邻判断会把 ``第 3 条是退款说明`` 里的
#: ``3`` 绑到"退款"上。值在前时不参与绑定，数字仍要过"每个数字都要有出处"那条。
_AFTER = 12


def _amounts(snapshot: dict) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for field in AMOUNT_FIELDS:
        raw = snapshot.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            number = Decimal(str(raw))
        except InvalidOperation:
            continue
        if number.is_finite():
            values[field] = number
    return values


def _numbers(text: str) -> list[tuple[Decimal, int, int]]:
    found = []
    for match in _NUMBER.finditer(text):
        try:
            found.append((Decimal(match.group(0)), match.start(), match.end()))
        except InvalidOperation:  # pragma: no cover - 正则保证可解析
            continue
    return found


def currency_claims(text: str) -> set[Decimal]:
    """正文里**带货币单位**的数字。它们是价格断言，不是计数。"""
    found = set()
    for match in CURRENCY.finditer(text):
        try:
            found.add(Decimal(match.group(1)))
        except InvalidOperation:  # pragma: no cover
            continue
    return found


def _nearest(numbers, text: str, start: int, end: int):
    """标签**后面**最近的数字。

    子句边界（句号、逗号、换行）就是断点：跨过它的数字属于另一句话。
    """
    window = text[end : end + _AFTER]
    cut = next((i for i, ch in enumerate(window) if ch in _CLAUSE_BREAK), len(window))
    after = [n for n, s, _ in numbers if end <= s <= end + cut]
    return after[0] if after else None


def _amount_numbers(text: str, snapshot: dict):
    """正文里的数字，**先挖掉订单号**再扫。

    订单号就写在金额标签附近（``订单号 ORDER-A-1，账上记的支付金额``），而它自己的数字
    （这里的 ``1``）会被当成"离「支付」最近的数字"—— 那是**另一个事实**，不是金额。
    按字符等长掩码而不是过滤 token：订单号可能不是纯数字（``ORDER-A-1``），过滤整串
    匹配不到它内部的数字。
    """
    order_no = str(snapshot.get("order_no") or "")
    masked = text.replace(order_no, "\0" * len(order_no)) if order_no else text
    return [
        (value, start, end)
        for value, start, end in _numbers(masked)
        # 19 位的数字不是任何金额可能的样子。
        if len(masked[start:end]) < 13
    ]


def _is_state_label(text: str, end: int) -> bool:
    tail = text[end : end + 4].lstrip("：: ")
    return any(tail.startswith(word) for word in NOT_AMOUNT)


def check_amount_labels(text: str, snapshot: dict) -> None:
    """金额必须挂在它自己的标签上。互换了就是互换 —— 两个值都为真也一样。"""
    values = _amounts(snapshot)
    numbers = _amount_numbers(text, snapshot)
    for field, labels in AMOUNT_LABELS.items():
        expected = values.get(field)
        for label in labels:
            for match in re.finditer(re.escape(label), text, re.IGNORECASE):
                if _is_state_label(text, match.end()):
                    continue
                found = _nearest(numbers, text, match.start(), match.end())
                if found is None:
                    continue
                if expected is None:
                    # 标签有、字段没有：这个数字没有出处，是编的。
                    raise ChatRunUnavailable(
                        "CHATFLOW_FACT_LABEL_MISMATCH",
                        f"reply states {label} but the snapshot has no {field}",
                    )
                if found != expected:
                    raise ChatRunUnavailable(
                        "CHATFLOW_FACT_LABEL_MISMATCH",
                        f"reply binds {label} to a value that is not the recorded {field}",
                    )


def check_currency(text: str, snapshot: dict) -> None:
    """带货币单位的数字只能是快照里的金额。``每小时 3 元`` 在这里被拦下。"""
    known = set(_amounts(snapshot).values())
    for value in sorted(currency_claims(text) - known):
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_INVENTED_AMOUNT",
            f"reply states a currency amount ({value}) that is not in the evidence",
        )


def covers_zero(text: str, field: str) -> bool:
    """金额为 0 时，正文用否定句说了这件事（``没有退款记录``）。

    这不是放宽“该有的都在”，而是承认 ``0`` 与“没有”是同一个事实的两种说法：
    快照说 ``recorded_refund_amount=0``，正文说“没有退款记录”已经把它说完了。
    """
    pattern = ZERO_SAID.get(field)
    return bool(pattern and pattern.search(text))


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[max(0, start - 12) : start]))


def check_promises(text: str) -> None:
    """不承诺到账、退款、扣款：系统只有业务记录，没有支付渠道回执。"""
    for match in PROMISE.finditer(text):
        if _negated(text, match.start()):
            continue
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_PROMISE",
            "reply promises a payment outcome the business record does not carry",
        )


def check_status(text: str, snapshot: dict) -> None:
    """状态不能说反：快照说结束，正文不能说"未结束"；快照说充电中，不能说"已结束"。"""
    role = str(snapshot.get("status_role") or "")
    if role not in {"finished", "charging"}:
        return
    for word in FINISHED_WORDS:
        for match in re.finditer(re.escape(word), text, re.IGNORECASE):
            negated = _negated(text, match.start())
            if role == "finished" and negated:
                raise ChatRunUnavailable(
                    "CHATFLOW_FACT_STATUS_INVERTED",
                    f"reply negates the recorded status ({word})",
                )
            if role == "charging" and not negated:
                raise ChatRunUnavailable(
                    "CHATFLOW_FACT_STATUS_INVERTED",
                    f"reply claims the order is {word} while it is still charging",
                )


def check(text: str, snapshot: dict) -> None:
    """四条判据一起跑。任何一条不过都是**整轮失败**，不降级成模板。"""
    check_amount_labels(text, snapshot)
    check_currency(text, snapshot)
    check_promises(text)
    check_status(text, snapshot)
