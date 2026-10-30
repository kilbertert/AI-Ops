"""回答正文是给人读的：分段、有小标题、不把内部标识摆给用户。

前端按 `blocks[]` 分派渲染、**不解析 Markdown**（`docs/agents/frontend-api-brief.md` §9），
所以"好读"只能靠纯文本排版表达。这里钉住三件在 2026-10-29 反馈里坏掉的事：

1. 列表要有编号/项目符号，而不是每项占一个空行；
2. 要有小标题，读的人才知道哪句是结论、哪句是下一步；
3. 内部接口标识与微秒 ISO 时间不直接出现在用户可见正文里。

**不比对整段文案**：那样每次改措辞都要改测试，最后测试会变成文案的副本。
断言的是结构特征与"不该出现的东西"。
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.consumer.consumer_billing import billing_answer
from aiops_diagnostics.consumer.consumer_order_chat import status_answer
from aiops_diagnostics.consumer.consumer_time import readable_time
from aiops_diagnostics.operator.operator_cpms import render_report

ISO = "2026-10-29T12:34:45.123456+00:00"

STATUS = {
    "order_no": "2113549588485718017",
    "status_code": "1",
    "status_role": "finished",
    "observed_at": ISO,
    "source": "cpms.chargingInfo",
}
BILLING = {
    **STATUS,
    "source": "cpms.orderDetail",
    "order_amount": "27.36",
    "recorded_payment_amount": "0.00",
    "refund_status": "0",
    "recorded_refund_amount": "0.00",
}
ANSWER = {
    "direct_answer": "合计 27.36 元，其中电费 13.68 元、服务费 13.68 元。",
    "summary": "订单已结束，停止原因为 Remote（远程停止）。",
    "probable_cause": "现有记录未保存操作人信息，无法确认发起方。",
    "next_steps": ["核对远程停止操作记录", "调取邻近时段样本做功率对比"],
    "limitations": ["监测样本返回 0 条，属缺证状态"],
}
EVIDENCE = {
    "cpms_order_detail": {"source": "cpms.orderDetail", "observed_at": ISO},
    "cpms_charging_samples": {"source": "cpms.chargingMonitor", "observed_at": ISO},
}


def test_time_is_readable_and_parse_failure_passes_through():
    assert readable_time(ISO) == "2026-10-29 12:34:45 UTC"
    assert readable_time("not-a-time") == "not-a-time"
    # 没有时区的值不硬加 UTC：那是把本地时间伪装成 UTC。
    assert readable_time("2026-10-29T12:34:45") == "2026-10-29 12:34:45"


@pytest.mark.parametrize("language", ["zh", "zh-Hant", "en"])
@pytest.mark.parametrize("render", [status_answer, billing_answer])
def test_consumer_answers_are_labelled_lines_not_one_sentence(render, language):
    text = render(dict(STATUS if render is status_answer else BILLING), language)["blocks"][0]["text"]
    # 逐行：至少三行非空，而不是一整段被分号串起来的话。
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 4, text
    # 时间可核对，但不出微秒 ISO 串。
    assert ISO not in text
    assert "2026-10-29 12:34:45" in text and "UTC" in text
    # 数据与免责说明分开成段，读的人不会把注意项当成数据。
    assert "\n\n" in text


def test_operator_report_has_headings_numbered_steps_and_human_sources():
    text = render_report(ANSWER, EVIDENCE)
    # 第一段就是回答用户问的那一点。
    assert text.startswith(ANSWER["direct_answer"])
    for heading in ("【订单事实】", "【可能原因】", "【建议下一步】", "【证据缺口】", "【数据来源】"):
        assert heading in text
    # 列表带编号：用户会说"第 2 条"，而且能和正文对上。
    assert "1. 核对远程停止操作记录" in text
    assert "2. 调取邻近时段样本做功率对比" in text
    # 来源给人话，不给内部接口标识；时间不带微秒。
    assert "订单详情" in text and "充电监测样本" in text
    assert "cpms.orderDetail" not in text and "cpms.chargingMonitor" not in text
    assert ISO not in text


def test_operator_report_omits_empty_sections_instead_of_printing_blank_headings():
    thin = {
        "direct_answer": "订单已结束。",
        "summary": "",
        "probable_cause": "",
        "next_steps": [],
        "limitations": [],
    }
    text = render_report(thin, {})
    assert text == "订单已结束。"
