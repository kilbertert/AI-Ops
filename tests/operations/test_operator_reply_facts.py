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


# --- 2026-11-02 反馈：后台「费用信息」栏的字段必须出现在 AI 答复里 ------------------

#: 截图那一栏的完整后台行。**先进投影，再进渲染** —— 手写证据会让字段表的改动
#: 绕过这条测试（变异验证：从 `evidence_fields` 删掉 launchFee，手写证据仍然全绿）。
FEE_ROW = {
    "orderNo": "2116050516255272961",
    "siteId": "SITE-1",
    "status": 1,
    "isPrePay": True,
    "prePayAmount": "10.00",
    "electricityFee": "5.13",
    "serviceFee": "1.71",
    "dsElectricFee": "0.20",
    "dsServiceFee": "0.10",
    "launchFee": "1.00",
    "occupyFee": "0.50",
    "marketAmount": "0",
    "vat": "2",
    "vatFee": "0.16",
    "totalAmount": "7.84",
    "payAmount": "8.00",
    "payTime": "2026-10-30T14:12:13",
    "refundAmount": "2.00",
}


def test_every_backend_fee_field_reaches_the_answer():
    """后台看得到的费用字段，AI 的答复里必须看得到（截图反馈的第 1 条）。

    这条覆盖的是**投影 + 渲染**两处：字段没进 `evidence_fields` 就进不了证据；
    进了证据但 `_order_fact_text` 不认识它，答复里仍然只有电费和服务费。
    所以证据必须**由 `project_evidence` 产出**，不是手写一份。
    """
    from aiops_diagnostics.operator.operator_cpms import project_evidence

    body = {
        "code": 0,
        "data": {"orderNo": FEE_ROW["orderNo"], "siteId": "SITE-1", "chOrderInfoModel": FEE_ROW},
    }
    evidence = {"cpms_order_detail": project_evidence(body, FEE_ROW["orderNo"], "cpms_order_detail")}
    text = render_report({"reply": "{{fact:order_facts}}"}, evidence)
    for label, value in [
        ("启动费用", "1.00"),
        ("优惠金额", "0"),
        ("增值税比例", "2"),
        ("增值税费用", "0.16"),
        ("预付费", "10.00"),
        ("电损电费", "0.20"),
        ("电损服务费", "0.10"),
        ("占位费", "0.50"),
    ]:
        assert f"{label}：{value}" in text, f"答复里没有「{label}：{value}」这一行"
    # 布尔值写成人话，不把 `True` 印给用户。
    assert "是否预付费：是" in text and "True" not in text.split("【数据来源】")[0]
    # 付款时间按可读写法，不把 CPMS 的 ISO 串原样摆出来。
    assert "系统记录的付款时间：2026-10-30 14:12:13" in text


def test_fee_lines_are_omitted_when_the_backend_has_no_such_column():
    """上游没有这一列时不补零、不补空：缺字段与「金额是 0」是两件事。"""
    evidence = {
        "cpms_order_detail": {
            "source": "cpms.orderDetail",
            "order_no": "SYNTHETIC-ORDER",
            "observed_at": "2026-11-02T02:08:18Z",
            "data": {"orderNo": "SYNTHETIC-ORDER", "status": 1, "electricityFee": "5.13", "launchFee": "0"},
        }
    }
    text = render_report({"reply": "{{fact:order_facts}}"}, evidence)
    # 上游真的给了 0，就写 0。
    assert "启动费用：0" in text
    # 上游没有这个字段，就不要这一行（不是「增值税费用：未提供」）。
    assert "增值税费用" not in text and "增值税比例" not in text


def test_a_frozen_report_is_not_rewritten_by_a_later_upgrade():
    """历史答案不能被升级后的渲染器改写：冻结文本优先。"""
    value = answer("我帮你核对了一下。\n{{fact:order_facts}}")
    frozen = "冻结的旧正文（中文标签）"
    snapshot = {"answer": value, "evidence": EVIDENCE, "language": "zh", "rendered_report": frozen}
    text = snapshot.get("rendered_report") or render_report(
        snapshot["answer"], snapshot["evidence"], language="zh"
    )
    assert text == frozen


def test_output_contract_tells_the_model_the_fact_block_carries_every_fee():
    """输出契约必须点明 `{{fact:order_facts}}` 里有费用区的各项金额（2026-11-02 反馈）。

    但它**不能把字段枚举摆在「不要复述」前面而不同时收紧措辞** —— 线上实测：模型把枚举
    当成可谈内容，开场就写金额/百分比，被 `check_notice` 判 `unsupported_fact` 整轮拒绝
    （3 次里 2 次）。所以契约里同时要有「其它行一个字都不要带数字」这一句。
    """
    from aiops_diagnostics.execution.node_publication import OPERATOR_OUTPUT

    for fee in ("启动费用", "优惠", "增值税"):
        assert fee in OPERATOR_OUTPUT, fee
    assert "一个字都不要带数字" in OPERATOR_OUTPUT
    assert "不要提具体是哪几项费用" in OPERATOR_OUTPUT
