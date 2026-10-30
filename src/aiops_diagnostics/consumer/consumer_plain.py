"""把订单状态与账务快照说成人话：先给结论，再逐项解释术语。

2026-10-30 的产品反馈是「内容不是技术解释，是下结论，然后给通俗易懂注释，最起码商务
能看得懂」。查到的形状不是排版问题（排版已在 ADR-0180 处理），而是**每一条都在罗列
字段**：正文没有一句「这单怎么样」，术语（`支付金额`／`未提供`／`status_code`／
`cpms.orderDetail`）原样摆给读者。

这里只放**词表与拼装**，不查数据、不做判断：数字全部来自调用方传进来的快照，缺就是缺。
与 ADR-0083 同一条纪律 —— 这一层也不许凭空产生金额。

为什么是独立模块：`consumer_order_chat`、`consumer_billing`、`consumer_language` 三条
渲染路都要用同一套词，而 `consumer_language` 被前两者导入。让它们互相 import 会在包内
形成循环边（`tests/test_readme_architecture.py` 会红）。这里只依赖 `consumer_time`
（另一个叶子），依赖方向始终单向。

**语种分工**：`zh`／`zh-Hant`／`en` 在这里拼；其余八种走 `consumer_language.json` 的
模板。加一种语言要同时改两处 —— 这是既有分工，不是本模块引入的。
"""

from __future__ import annotations

from aiops_diagnostics.consumer.consumer_time import readable_time

#: 内部接口标识 → 读者能核对的来源名。与管家报告 `_SOURCE_LABELS` 同一套说法：
#: 用户不该看见 `cpms.orderDetail`，但「来源」本身必须留着 —— 它是这段话可以拿去
#: 核对的凭据，也是"这段话从哪来"的答案。
SOURCE_KEYS = {
    "cpms.chargingInfo": "source_charging",
    "cpms.orderDetail": "source_billing",
}

#: 退款状态码 → 人话。缺值（None／未知码）单列一句，不猜成「未退款」：
#: 「没有这条记录」与「有记录表示没退」是两件事，混起来会让用户以为查过了。
REFUND_CODES = {"0": "refund_0", "1": "refund_1", "2": "refund_2"}

WORDS: dict[str, dict[str, str]] = {
    "zh": {
        "sep": "，",
        "colon": "：",
        "status_lede": "这单当前的状态是「{status}」。",
        "status_order": "订单号",
        "status_time": "查询于",
        "status_note": "这只是我查到的业务状态，不代表故障原因。",
        "status_unknown_note": "这个状态码不在已知范围内，我无法确认它代表什么。",
        "source_line": "来源：{source}",
        "source_charging": "充电状态记录",
        "source_billing": "订单账务记录",
        "source_unknown": "业务记录",
        "status_unknown": "未知",
        "refund_0": "没有退款记录",
        "refund_1": "有全额退款记录",
        "refund_2": "有部分退款记录",
        "refund_unknown": "退款状态待核实",
        "billing_lede": "这单的账单{parts}。",
        "billing_lede_empty": "这单的账单金额，上游接口一个都没有给。",
        "billing_part_amount": "合计 {value}",
        "billing_part_paid": "系统记录的支付金额是 {value}",
        "billing_item_amount": "· 订单金额 {value} —— 这单的总计费",
        "billing_item_paid": "· 支付金额 {value} —— 账务系统里记的数，不等于银行已经扣款",
        "billing_item_refund": "· 退款：{refund} —— 有这条记录也不代表银行已经到账",
        "billing_refunded": "，已退金额 {value}",
        "billing_missing": "上游未提供",
        "billing_missing_suffix": "；",
        "billing_missing_note": "上游接口没有给这个字段，需到原订单页面核对",
        "billing_currency_suffix": "；",
        "billing_currency_note": "币种接口未提供，金额单位请与原订单页面核对",
        "billing_disclaimer": "我没有发起任何付款或退款，只能读取记录。",
        "billing_order": "订单号",
    },
    "zh-Hant": {
        "sep": "，",
        "colon": "：",
        "status_lede": "這筆訂單目前的狀態是「{status}」。",
        "status_order": "訂單號",
        "status_time": "查詢於",
        "status_note": "這只是我查到的業務狀態，不代表故障原因。",
        "status_unknown_note": "這個狀態碼不在已知範圍內，我無法確認它代表什麼。",
        "source_line": "來源：{source}",
        "source_charging": "充電狀態記錄",
        "source_billing": "訂單帳務記錄",
        "source_unknown": "業務記錄",
        "status_unknown": "未知",
        "refund_0": "沒有退款記錄",
        "refund_1": "有全額退款記錄",
        "refund_2": "有部分退款記錄",
        "refund_unknown": "退款狀態待核實",
        "billing_lede": "這筆訂單的帳單{parts}。",
        "billing_lede_empty": "這筆訂單的帳單金額，上游介面一個都沒有提供。",
        "billing_part_amount": "合計 {value}",
        "billing_part_paid": "系統記錄的付款金額是 {value}",
        "billing_item_amount": "· 訂單金額 {value} —— 這筆訂單的總計費",
        "billing_item_paid": "· 付款金額 {value} —— 帳務系統裡記的數，不等於銀行已經扣款",
        "billing_item_refund": "· 退款：{refund} —— 有這條記錄也不代表銀行已經到帳",
        "billing_refunded": "，已退金額 {value}",
        "billing_missing": "上游未提供",
        "billing_missing_suffix": "；",
        "billing_missing_note": "上游介面沒有提供這個欄位，需到原訂單頁面核對",
        "billing_currency_suffix": "；",
        "billing_currency_note": "幣別介面未提供，金額單位請與原訂單頁面核對",
        "billing_disclaimer": "我沒有發起任何付款或退款，只能讀取記錄。",
        "billing_order": "訂單號",
    },
    "en": {
        "sep": ", ",
        "colon": ": ",
        "status_lede": "This order is currently “{status}”.",
        "status_order": "Order number",
        "status_time": "Checked at",
        "status_note": "This is only the business status I could read; it is not a diagnosis of the cause.",
        "status_unknown_note": "This status code is outside the known set, "
        "so I cannot confirm what it means.",
        "source_line": "Source: {source}",
        "source_charging": "charging status record",
        "source_billing": "order billing record",
        "source_unknown": "business record",
        "status_unknown": "unknown",
        "refund_0": "no refund recorded",
        "refund_1": "a full refund is recorded",
        "refund_2": "a partial refund is recorded",
        "refund_unknown": "refund status unverified",
        "billing_lede": "This order's bill lists {parts}.",
        "billing_lede_empty": "The upstream interface supplied none of the billing amounts.",
        "billing_part_amount": "a total of {value}",
        "billing_part_paid": "a recorded payment amount of {value}",
        "billing_item_amount": "- Order amount {value} — the total charged for this order",
        "billing_item_paid": "- Payment amount {value} — the figure recorded in the billing "
        "system, which does not prove a bank debit",
        "billing_item_refund": "- Refund: {refund} — a recorded refund does not prove the "
        "money reached the bank account",
        "billing_refunded": "; refunded amount {value}",
        "billing_missing": "not supplied upstream",
        "billing_missing_suffix": "; ",
        "billing_missing_note": "the upstream interface did not supply this field; "
        "check the original order page",
        "billing_currency_suffix": "; ",
        "billing_currency_note": "no currency was supplied, so check the unit on the original order page",
        "billing_disclaimer": "I did not initiate any payment or refund; I can only read records.",
        "billing_order": "Order number",
    },
}

#: 简体状态标签 → 繁体。只覆盖公司状态表里实际出现的词，不做通用转换：
#: 一个"看起来像繁体"的机械转换会改掉用户认得的术语。
_TRADITIONAL_STATUS = {"充电中": "充電中", "充电结束": "充電結束", "未知": "未知"}


def words(language: str) -> dict[str, str]:
    """取词表。未收录的语种退回英文 —— 与既有实现同样的降级方向。"""
    if language in WORDS:
        return WORDS[language]
    return WORDS["zh-Hant"] if language.startswith("zh") else WORDS["en"]


def source_label(language: str, source: object) -> str:
    """内部来源标识换成人话。不认识的来源不猜含义，只说「业务记录」。"""
    table = words(language)
    return table[SOURCE_KEYS.get(str(source), "source_unknown")]


def refund_text(language: str, refund_status: object) -> str:
    table = words(language)
    return table[REFUND_CODES.get(str(refund_status), "refund_unknown")]


def _amount(value: object) -> str | None:
    """只在**上游真的给了数**时返回值。None 与空串都算没给，`"0"` 是给了。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _status_name(language: str, snapshot: dict) -> str:
    from aiops_diagnostics.health.rules import status_label as business_status_label

    table = words(language)
    role = snapshot["status_role"]
    if language == "en":
        return str(role).replace("_", " ")
    if role == "unknown":
        return table["status_unknown"]
    label = business_status_label(snapshot["status_code"])
    if language == "zh-Hant":
        return _TRADITIONAL_STATUS.get(label, label)
    return label


def status_text(language: str, snapshot: dict) -> str:
    table = words(language)
    colon = table["colon"]
    note = table["status_note"]
    if snapshot["status_role"] == "unknown":
        note += table["status_unknown_note"]
    return "\n".join(
        [
            table["status_lede"].format(status=_status_name(language, snapshot)),
            "",
            f"{table['status_order']}{colon}{snapshot['order_no']}",
            f"{table['status_time']}{colon}{readable_time(snapshot['observed_at'])}",
            "",
            note,
            table["source_line"].format(source=source_label(language, snapshot["source"])),
        ]
    )


def billing_text(language: str, snapshot: dict) -> str:
    table = words(language)
    refund = refund_text(language, snapshot["refund_status"])
    amount = _amount(snapshot["order_amount"])
    paid = _amount(snapshot["recorded_payment_amount"])
    refunded = _amount(snapshot["recorded_refund_amount"])

    # 结论句只由**真实存在的值**拼：缺的那一项不写进结论，也不给它编一个数字。
    parts = []
    if amount is not None:
        parts.append(table["billing_part_amount"].format(value=amount))
    if paid is not None:
        parts.append(table["billing_part_paid"].format(value=paid))
    parts.append(refund)
    lede = (
        table["billing_lede"].format(parts=table["sep"].join(parts)) if parts else table["billing_lede_empty"]
    )

    amount_item = table["billing_item_amount"].format(value=amount or table["billing_missing"])
    paid_item = table["billing_item_paid"].format(value=paid or table["billing_missing"])
    refund_item = table["billing_item_refund"].format(refund=refund)
    # 缺值要写成「上游没有这个字段」，而不是让读者以为我们没查。
    if amount is None:
        amount_item += table["billing_missing_suffix"] + table["billing_missing_note"]
    else:
        amount_item += table["billing_currency_suffix"] + table["billing_currency_note"]
    if paid is None:
        paid_item += table["billing_missing_suffix"] + table["billing_missing_note"]
    if refunded is not None:
        refund_item += table["billing_refunded"].format(value=refunded)

    source = source_label(language, snapshot["source"])
    return "\n".join(
        [
            lede,
            "",
            amount_item,
            paid_item,
            refund_item,
            "",
            table["billing_disclaimer"],
            "",
            # 订单号必须在正文里：没有它，读者无法核对这是哪一单，前面所有数字都无从对照。
            f"{table['billing_order']}{table['colon']}{snapshot['order_no']}",
            f"{table['source_line'].format(source=source)} · {readable_time(snapshot['observed_at'])}",
        ]
    )
