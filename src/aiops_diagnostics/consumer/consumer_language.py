"""C端固定提示与事实模板；不在运行时调用模型翻译。"""

import json
from pathlib import Path

COPY = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def live_answer(snapshot, language, kind):
    pack = COPY[language]
    values = dict(snapshot)
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
