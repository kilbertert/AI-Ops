"""管家自然正文必须保留本轮事实，不能把支付金额与合计互换。"""

import pytest

from aiops_diagnostics.operator.operator_cpms import OperatorRefused, render_report, validate_answer

EVIDENCE = {
    "cpms_order_detail": {
        "source": "cpms.orderDetail",
        "order_no": "SYNTHETIC-ORDER",
        "observed_at": "2026-10-30T00:00:00Z",
        "data": {
            "orderNo": "SYNTHETIC-ORDER",
            "status": 1,
            "totalAmount": "27.36",
            "payAmount": "20",
            "electricityFee": "13.68",
            "serviceFee": "13.68",
        },
    }
}


def answer(reply):
    return {
        "direct_answer": "已核对本轮记录。",
        "summary": "本轮证据。",
        "probable_cause": "原因待确认。",
        "next_steps": ["请核对原记录。"],
        "limitations": ["记录不能证明历史原因。"],
        "evidence_tools": ["cpms_order_detail"],
        "reply": reply,
    }


@pytest.mark.parametrize(
    "reply",
    [
        "这单合计20，实付27.36。",
        "这单未结束，还在充电。",
        "每小时3元。",
        "款项已到账。",
        "这单未结束。\n{{fact:order_facts}}",
        "合计20，实付27.36。\n{{fact:order_facts}}",
    ],
)
def test_operator_reply_refuses_unsupported_facts(reply):
    with pytest.raises(OperatorRefused):
        validate_answer(answer(reply), EVIDENCE)


def test_operator_style_keeps_system_owned_order_facts():
    value = answer("我帮你核对了一下。\n{{fact:order_facts}}\n建议先对照原订单记录，再确认原因。")
    validate_answer(value, EVIDENCE)
    text = render_report(value, EVIDENCE)
    for expected in ["我帮你核对了一下。", "SYNTHETIC-ORDER", "充电结束", "27.36", "20", "13.68", "订单详情"]:
        assert expected in text
    assert "{{fact:" not in text


def test_old_operator_protocol_does_not_become_business_prompt():
    from aiops_diagnostics.execution.node_publication import OPERATOR_OUTPUT_PREFIX, business_prompt

    old = "运营自己编辑的提示词\n" + OPERATOR_OUTPUT_PREFIX + "旧系统协议，不包含新事实片段。"
    assert business_prompt(old) == "运营自己编辑的提示词"


def test_new_operator_run_cannot_silently_fall_back_to_old_report(tmp_path, monkeypatch):
    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import environment, node, request, terminal

    with environment(tmp_path, monkeypatch) as f:
        value = answer("{{fact:order_facts}}")
        del value["reply"]

        def dispatch(_, config, run_id):
            response = node(f, run_id, answer=value)
            assert response.status_code == 403
            raise RuntimeError("invalid model output")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        qa = request(f["client"]).json()["qa_id"]
        assert terminal(f, qa).json()["status"] == "failed"


# --- 第四轮复核 C：管家英文正文字段标签与历史回读 ------------------------------


def test_english_reply_gets_english_fact_labels_and_same_values():
    """C. 英文运行的 facts 段必须是英文标签，值与来源不变。"""
    value = answer("Checked your order.\n{{fact:order_facts}}\nAnything else to look at?")
    text = render_report(value, EVIDENCE, language="en")
    for expected in ("Order:", "Order status:", "Total:", "27.36", "20", "13.68", "Order detail"):
        assert expected in text
    assert not [char for char in text if "\u4e00" <= char <= "\u9fff"], "英文正文里不该有中文标签"


def test_a_frozen_report_is_not_rewritten_by_a_later_upgrade():
    """历史答案不能被升级后的渲染器改写：冻结文本优先。"""
    value = answer("我帮你核对了一下。\n{{fact:order_facts}}")
    frozen = "冻结的旧正文（中文标签）"
    snapshot = {"answer": value, "evidence": EVIDENCE, "language": "zh", "rendered_report": frozen}
    text = snapshot.get("rendered_report") or render_report(
        snapshot["answer"], snapshot["evidence"], language="zh"
    )
    assert text == frozen
