"""C端固定提示与事实模板；不在运行时调用模型翻译。"""

import json
from pathlib import Path

COPY = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def live_answer(snapshot, language, kind):
    from aiops_diagnostics.consumer.consumer_plain import SOURCE_KEYS
    from aiops_diagnostics.consumer.consumer_time import readable_time

    pack = COPY[language]
    values = dict(snapshot)
    # 时间在**两条渲染路**上必须是同一种写法：`localized=True` 走这里的模板，
    # 未本地化走 `consumer_order_chat`／`consumer_billing` 的函数。只改一边会让同一份
    # 事实按语言不同显示出不同精度 —— 用户对比两种语言时会以为数据不一样。
    values["observed_at"] = readable_time(snapshot["observed_at"])
    # 来源也换成人话：`cpms.orderDetail` 是内部标识，不该出现在用户正文里。
    # 标签从**本语种的语言包**取，不从中文词表取 —— 否则德语正文里会冒出英文标签。
    values["source"] = pack[SOURCE_KEYS.get(str(snapshot["source"]), "source_unknown")]
    if kind == "status":
        values["status"] = pack.get(snapshot["status_role"], pack["unknown"])
    else:
        values["refund"] = pack.get("refund_" + str(snapshot["refund_status"]), pack["refund_unknown"])
        for key in ("order_amount", "recorded_payment_amount", "recorded_refund_amount"):
            values[key] = pack["missing"] if snapshot[key] is None else snapshot[key]
    return {
        "blocks": [{"kind": "text", "text": pack[kind].format(**values)}],
        "retrieval_status": "not_found",
    }
