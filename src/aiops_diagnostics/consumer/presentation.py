"""C 端答复渲染、多语言文案包与状态查询规则的唯一展示层。

将原 ``consumer_time``、``consumer_language``、``consumer_plain``、``consumer_copy``
与 ``consumer_order_chat`` 合并为单一叶子模块，消除内部跨文件碎片与人为规避循环的拆分：

* **时间渲染**（:func:`readable_time`）：将 ISO-8601 取证时间格式化为读者可核对的文本。
* **状态与账务文案**（``COPY`` / ``WORDS`` / :func:`status_word` / :func:`status_text` /
  :func:`billing_text`）：先给结论再解释术语，数字一律来自上游快照，绝不编造。
* **租户文案包**（:func:`validate_copy_bundle` / :func:`copy_pack` / :func:`resolve_copy_pack`）：
  允许按租户定制措辞并按版本复现，但不允许删改占位符。
* **订单状态问答入口**（:func:`is_order_status_question` / :func:`asks_latest_order` /
  :func:`status_answer`）。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter
from typing import Any

from aiops_diagnostics.common.i18n import language_spec, localized_pack, rendering_language
from aiops_diagnostics.data.order_status import StatusRole, role_of
from aiops_diagnostics.health.rules import ORDER_STATUS

# ---------------------------------------------------------------------------
# 1. 取证时间展示
# ---------------------------------------------------------------------------


def readable_time(iso: object) -> str:
    """将 ISO-8601 取证时间渲染成人类易核对格式；解析失败原样返回。"""
    try:
        parsed = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso)
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


# ---------------------------------------------------------------------------
# 2. 多语言固定模板与状态词表
# ---------------------------------------------------------------------------

COPY = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))

#: 中文状态标签按**角色**索引。`ORDER_STATUS` 是「这个 code 叫什么」的权威，
#: 而 `role_of` 是「这个 code 是什么角色」的权威；两者都是一对一，所以这张
#: 反查表与它们同构，不是第三份标签表。`unknown` 不在 `ORDER_STATUS` 里，
#: 单独补一句 —— 它与 `status_label` 对未知码的「未知」逐字一致。
_ZH_STATUS_WORDS: dict[str, str] = {role_of(code).value: label for code, label in ORDER_STATUS.items()}
_ZH_STATUS_WORDS[StatusRole.UNKNOWN.value] = "未知"


def status_word(language: str, role: str) -> str:
    """一个状态角色在某门语言里的说法。**唯一权威**。"""
    table = COPY.get(language)
    if table is not None and role in table:
        return table[role]
    spec = language_spec(language)
    if spec is not None and spec.is_chinese:
        return _ZH_STATUS_WORDS.get(role, _ZH_STATUS_WORDS[StatusRole.UNKNOWN.value])
    return role.replace("_", " ")


# ---------------------------------------------------------------------------
# 3. 组合式词表与人话拼装
# ---------------------------------------------------------------------------

#: 内部接口标识 → 读者能核对的来源名。
SOURCE_KEYS = {
    "cpms.chargingInfo": "source_charging",
    "cpms.orderDetail": "source_billing",
}

#: 退款状态码 → 人话。缺值（None／未知码）单列一句，不猜成「未退款」。
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


def words(language: str, pack: dict[str, str] | None = None) -> dict[str, str]:
    """取词表。`pack` 是租户文案包（已合并内置默认），None 就用内置文案。"""
    if pack is not None:
        return pack
    return localized_pack(WORDS, language)


def source_label(language: str, source: object, pack: dict[str, str] | None = None) -> str:
    """内部来源标识换成人话。不认识的来源不猜含义，只说「业务记录」。"""
    table = words(language, pack)
    return table[SOURCE_KEYS.get(str(source), "source_unknown")]


def refund_text(language: str, refund_status: object, pack: dict[str, str] | None = None) -> str:
    table = words(language, pack)
    return table[REFUND_CODES.get(str(refund_status), "refund_unknown")]


def _raw_amount_text(value: object) -> str | None:
    """只在**上游真的给了数**时返回值。None 与空串都算没给，`"0"` 是给了。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def live_answer(snapshot: dict[str, Any], language: str, kind: str, *, pack: dict[str, str] | None = None):
    """使用多语言整段模板（``presentation.json``）渲染实时状态或账务答复。"""
    words_map = localized_pack(COPY, language) if pack is None else {**localized_pack(COPY, language), **pack}
    values = dict(snapshot)
    values["observed_at"] = readable_time(snapshot["observed_at"])
    values["source"] = words_map[SOURCE_KEYS.get(str(snapshot["source"]), "source_unknown")]
    if kind == "status":
        values["status"] = words_map.get(snapshot["status_role"], words_map["unknown"])
    else:
        values["refund"] = words_map.get(
            "refund_" + str(snapshot["refund_status"]), words_map["refund_unknown"]
        )
        for key in ("order_amount", "recorded_payment_amount", "recorded_refund_amount"):
            values[key] = words_map["missing"] if snapshot[key] is None else snapshot[key]
    return {
        "blocks": [{"kind": "text", "text": words_map[kind].format(**values)}],
        "retrieval_status": "not_found",
    }


def status_text(
    language: str,
    snapshot: dict[str, Any],
    pack: dict[str, str] | None = None,
    *,
    status_word: str,
) -> str:
    table = words(language, pack)
    colon = table["colon"]
    note = table["status_note"]
    if snapshot["status_role"] == "unknown":
        note += table["status_unknown_note"]
    return "\n".join(
        [
            table["status_lede"].format(status=status_word),
            "",
            f"{table['status_order']}{colon}{snapshot['order_no']}",
            f"{table['status_time']}{colon}{readable_time(snapshot['observed_at'])}",
            "",
            note,
            table["source_line"].format(source=source_label(language, snapshot["source"], pack)),
        ]
    )


def billing_text(language: str, snapshot: dict[str, Any], pack: dict[str, str] | None = None) -> str:
    table = words(language, pack)
    refund = refund_text(language, snapshot["refund_status"], pack)
    amount = _raw_amount_text(snapshot["order_amount"])
    paid = _raw_amount_text(snapshot["recorded_payment_amount"])
    refunded = _raw_amount_text(snapshot["recorded_refund_amount"])

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
    if amount is None:
        amount_item += table["billing_missing_suffix"] + table["billing_missing_note"]
    else:
        amount_item += table["billing_currency_suffix"] + table["billing_currency_note"]
    if paid is None:
        paid_item += table["billing_missing_suffix"] + table["billing_missing_note"]
    if refunded is not None:
        refund_item += table["billing_refunded"].format(value=refunded)

    source = source_label(language, snapshot["source"], pack)
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
            f"{table['billing_order']}{table['colon']}{snapshot['order_no']}",
            f"{table['source_line'].format(source=source)} · {readable_time(snapshot['observed_at'])}",
        ]
    )


# ---------------------------------------------------------------------------
# 4. 租户文案包校验与版本解析（原 consumer_copy）
# ---------------------------------------------------------------------------

#: 一条文案的上限。
MAX_COPY_CHARS = 400

#: 可以被文案包覆盖的语种（组合式小键词表覆盖的语种）。
COMPOSED_LANGUAGES = ("zh", "en")


class CopyBundleInvalid(ValueError):
    """文案包不合法。错误文本要能直接告诉发布者改哪一条。"""


def _format_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


def bundled_copy(language: str) -> dict[str, str]:
    """内置文案（代码里的默认）。未知语种的回退方向由 `i18n.localized_pack` 统一回答。"""
    return words(language)


def validate_copy_bundle(bundle: Any) -> dict[str, dict[str, str]]:
    """校验并归一化一份租户文案包（``{"languages": {lang: {key: text}}}``）。"""
    if not isinstance(bundle, dict) or set(bundle) != {"languages"}:
        raise CopyBundleInvalid("文案包必须且只能有 languages 一个顶层字段")
    languages = bundle["languages"]
    if not isinstance(languages, dict) or not languages:
        raise CopyBundleInvalid("languages 必须是非空对象")
    parsed: dict[str, dict[str, str]] = {}
    for language, entries in languages.items():
        if language not in COMPOSED_LANGUAGES:
            raise CopyBundleInvalid(
                f"{language} 的正文还是整段模板，暂时不能改文案；"
                f"当前可改的语种：{', '.join(COMPOSED_LANGUAGES)}"
            )
        if not isinstance(entries, dict) or not entries:
            raise CopyBundleInvalid(f"{language} 必须是非空对象")
        default = bundled_copy(language)
        clean: dict[str, str] = {}
        for key, text in entries.items():
            if key not in default:
                raise CopyBundleInvalid(f"{language} 没有这个文案键：{key}")
            if not isinstance(text, str) or not text.strip():
                raise CopyBundleInvalid(f"{language}.{key} 不能为空")
            if len(text) > MAX_COPY_CHARS:
                raise CopyBundleInvalid(f"{language}.{key} 超过 {MAX_COPY_CHARS} 字（实际 {len(text)}）")
            try:
                text.format(**{name: "x" for name in _format_fields(text)})
            except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
                raise CopyBundleInvalid(f"{language}.{key} 的格式写法无法渲染：{type(exc).__name__}") from exc
            missing = _format_fields(default[key]) - _format_fields(text)
            extra = _format_fields(text) - _format_fields(default[key])
            if missing or extra:
                raise CopyBundleInvalid(
                    f"{language}.{key} 的占位符必须与内置文案一致："
                    f"缺 {sorted(missing) or '无'}，多 {sorted(extra) or '无'}"
                )
            clean[key] = text
        parsed[language] = clean
    return parsed


def copy_pack(language: str, overrides: dict[str, dict[str, str]] | None) -> dict[str, str]:
    """内置文案 + 该语种的覆盖项。没覆盖的语种拿到的就是内置文案本身。"""
    base = dict(bundled_copy(language))
    if overrides:
        base.update(overrides.get(language, {}))
    return base


def resolve_copy_pack(
    store: Any,
    tenant_id: str,
    *,
    version: int | None,
    language: str,
) -> dict[str, str] | None:
    """按钉住的版本取词表。非组合式语种返回 ``None`` 走原整段模板。"""
    if language not in COMPOSED_LANGUAGES:
        return None
    if not version:
        return bundled_copy(language)
    _, bundle = store.consumer_copy(tenant_id, int(version))
    return copy_pack(language, (bundle or {}).get("languages"))


# ---------------------------------------------------------------------------
# 5. 本人订单状态提问识别与渲染（原 consumer_order_chat）
# ---------------------------------------------------------------------------

_ORDER = re.compile(r"订单|訂單|一单|一單|\border\b", re.I)
_LATEST = re.compile(r"最新|最近|最后|最後|\b(?:latest|most recent|last)\b", re.I)
_STATUS = re.compile(r"状态|狀態|充电中|充電中|结束|結束|\b(?:status|charging|finished|ended)\b", re.I)
_QUERY = re.compile(r"查|查看|看看|\b(?:show|check|look up)\b", re.I)
_DIAGNOSIS = re.compile(
    r"异常|異常|故障|扣费|扣費|退款|金额|金額|投诉|投訴|原因|为什么|為什麼|检测|檢測|诊断|診斷"
    r"|支付|取消|删除|刪除|修改|停止|重算|补发|補發|政策|规则|規則"
    r"|\b(?:fault|refund|charged|fees?|cost|complaint|why|diagnos\w*|pay\w*|cancel|delete|change|stop|policy)\b",
    re.I,
)


def is_order_status_question(question: str) -> bool:
    """保守识别明确查询；未命中的措辞仍走原有路由。"""
    return bool(
        _ORDER.search(question)
        and (_STATUS.search(question) or (_LATEST.search(question) and _QUERY.search(question)))
        and not _DIAGNOSIS.search(question)
    )


def asks_latest_order(question: str) -> bool:
    return bool(_LATEST.search(question))


def status_answer(
    snapshot: dict[str, Any],
    language: str,
    *,
    localized: bool = False,
    pack: dict[str, str] | None = None,
) -> dict[str, Any]:
    """本人这次查询到的业务状态（整段模板或组合式人话渲染）。"""
    if localized and language in COPY:
        return live_answer(snapshot, language, "status", pack=pack)
    language = rendering_language(WORDS, language)
    text = status_text(language, snapshot, pack, status_word=status_word(language, snapshot["status_role"]))
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}
