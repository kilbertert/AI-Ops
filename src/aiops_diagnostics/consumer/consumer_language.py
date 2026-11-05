"""C端固定提示与事实模板；不在运行时调用模型翻译。"""

import json
from pathlib import Path

from aiops_diagnostics.common.i18n import language_spec, localized_pack
from aiops_diagnostics.data.order_status import StatusRole, role_of
from aiops_diagnostics.health.rules import ORDER_STATUS

COPY = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))

#: 中文状态标签按**角色**索引。`ORDER_STATUS` 是「这个 code 叫什么」的权威，
#: 而 `role_of` 是「这个 code 是什么角色」的权威；两者都是一对一，所以这张
#: 反查表与它们同构，不是第三份标签表。`unknown` 不在 `ORDER_STATUS` 里，
#: 单独补一句 —— 它与 `status_label` 对未知码的「未知」逐字一致。
_ZH_STATUS_WORDS: dict[str, str] = {role_of(code).value: label for code, label in ORDER_STATUS.items()}
_ZH_STATUS_WORDS[StatusRole.UNKNOWN.value] = "未知"


def status_word(language: str, role: str) -> str:
    """一个状态角色在某门语言里的说法。**唯一权威**。

    `COPY` 覆盖九门语言（含 `zh-Hant`）的全部角色；`zh` 与 `en` 走代码路径
    （中文标签 / 角色名），因为它们不在 `COPY` 里。消费者（状态正文、管家报告、
    回读重算）都调这里，不再各自回答「这门语言下这句怎么说」。

    键是**角色**而不是状态码：`data.order_status.role_of` 已是「码 → 角色」的唯一
    权威，这里只按同一个键取值，不新造映射。
    """
    table = COPY.get(language)
    if table is not None and role in table:
        return table[role]
    spec = language_spec(language)
    if spec is not None and spec.is_chinese:
        return _ZH_STATUS_WORDS.get(role, _ZH_STATUS_WORDS[StatusRole.UNKNOWN.value])
    return role.replace("_", " ")


def live_answer(snapshot, language, kind, *, pack=None):
    from aiops_diagnostics.consumer.consumer_plain import SOURCE_KEYS
    from aiops_diagnostics.consumer.consumer_time import readable_time

    # `pack` 是**合并后的词表**（内置 + 租户覆盖）。模板结构仍取自语言包：
    # 租户改的是词句，不是结构，所以占位符集合天然不变。
    words = localized_pack(COPY, language) if pack is None else {**localized_pack(COPY, language), **pack}
    values = dict(snapshot)
    # 时间在**两条渲染路**上必须是同一种写法：`localized=True` 走这里的模板，
    # 未本地化走 `consumer_order_chat`／`consumer_billing` 的函数。只改一边会让同一份
    # 事实按语言不同显示出不同精度 —— 用户对比两种语言时会以为数据不一样。
    values["observed_at"] = readable_time(snapshot["observed_at"])
    # 来源也换成人话：`cpms.orderDetail` 是内部标识，不该出现在用户正文里。
    # 标签从**本语种的语言包**取，不从中文词表取 —— 否则德语正文里会冒出英文标签。
    values["source"] = words[SOURCE_KEYS.get(str(snapshot["source"]), "source_unknown")]
    if kind == "status":
        values["status"] = words.get(snapshot["status_role"], words["unknown"])
    else:
        values["refund"] = words.get("refund_" + str(snapshot["refund_status"]), words["refund_unknown"])
        for key in ("order_amount", "recorded_payment_amount", "recorded_refund_amount"):
            values[key] = words["missing"] if snapshot[key] is None else snapshot[key]
    return {
        "blocks": [{"kind": "text", "text": words[kind].format(**values)}],
        "retrieval_status": "not_found",
    }
