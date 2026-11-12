"""用户批准的确定性事实交付：模型换数字/标签/状态不能进入最终答案。"""

import pytest

from aiops_diagnostics.consumer.consumer_agent import final_answer
from consumer.test_consumer_mall_billing import payload, project, snapshot


def context(*calls):
    return {"calls": [{"tool": tool, "result": result} for tool, result in calls]}


def billing():
    snap = snapshot()
    snap.update(
        source="cpms.orderDetail",
        observed_at="2026-11-12T00:00:00Z",
        currency=None,
        currency_status="missing",
    )
    snap["mall_payment"] = project(payload())
    return {"status": "found", "snapshot": snap, "order_ids": [snap["order_no"]]}


def test_wrong_model_money_and_status_cannot_enter_final_answer():
    ctx = context(("billing", billing()))
    wrong = "订单费用9.99元，充电正常结束，没有多扣款，税是我相减得到的。"
    answer = final_answer(wrong, ctx, language="zh")
    text = answer["blocks"][0]["text"]
    assert all(v in text for v in ("4.62", "4.71", "0.09"))
    assert "正常结束" not in text and "9.99" not in text and "元" not in text
    assert "无法确认" in text and "币种" in text
    assert answer == final_answer("另一段完全不同的模型回复", ctx, language="zh")


def test_mixed_knowledge_keeps_quote_and_discards_model_order_claims():
    source = {
        "retrieved_reference_id": "ref-a",
        "title": "帮助",
        "content": "请在订单页面查看记录。",
        "media": [],
    }
    ctx = context(("billing", billing()), ("knowledge", {"status": "found", "sources": [source]}))
    result = final_answer("充电正常结束", ctx, language="zh")
    assert "正常结束" not in str(result)
    assert "请在订单页面查看记录。" in str(result)
    assert any(b.get("reference_id") == "ref-a" for b in result["blocks"])


def test_pure_knowledge_still_uses_model_explanation():
    source = {
        "retrieved_reference_id": "ref-a",
        "title": "Help",
        "content": "Open the app first.",
        "media": [],
    }
    ctx = context(("knowledge", {"status": "found", "sources": [source]}))
    assert (
        "Open the app first." in final_answer("Open the app first.", ctx, language="en")["blocks"][0]["text"]
    )


def test_order_history_and_denied_reads_never_revive_old_facts():
    ctx = context(("billing", {"status": "refused"}))
    ctx["conversation"] = {
        "selected_order": "TEST-CHARGE-001",
        "history": [{"order_ids": ["TEST-CHARGE-001"], "answer": "费用9.99"}],
    }
    text = final_answer("费用9.99，正常结束", ctx, language="zh")["blocks"][0]["text"]
    assert "9.99" not in text and "正常结束" not in text
    assert "未取得" in text


def test_repeated_read_failure_does_not_reuse_earlier_success():
    result = billing()
    ctx = {
        "calls": [
            {"tool": "billing", "arguments": {"order_no": result["snapshot"]["order_no"]}, "result": result},
            {
                "tool": "billing",
                "arguments": {"order_no": result["snapshot"]["order_no"]},
                "result": {"status": "unavailable"},
            },
        ]
    }
    text = final_answer("正常结束", ctx)["blocks"][0]["text"]
    assert "4.62" not in text and "未取得" in text


def test_order_question_cannot_use_knowledge_to_invent_order_facts():
    source = {"retrieved_reference_id": "ref-a", "title": "帮助", "content": "查看订单。", "media": []}
    ctx = context(("knowledge", {"status": "found", "sources": [source]}))
    result = final_answer("这笔订单已正常结束", ctx, question="这笔订单是否异常？")
    assert "正常结束" not in str(result)


def test_large_stored_amount_is_not_redacted_or_recomputed():
    result = billing()
    result["snapshot"]["order_amount"] = "123456789012.12345678"
    text = final_answer("0", context(("billing", result)))["blocks"][0]["text"]
    assert "123456789012.12345678" in text


def test_localized_values_and_missing_zero_keep_their_meaning():
    result = billing()
    result["snapshot"]["fee_components"].update(parkFee="0.00", occupyFee=None)
    for language in ("en", "de", "zh-Hant"):
        text = final_answer("wrong", context(("billing", result)), language=language)["blocks"][0]["text"]
        assert "4.62" in text and "0.00" in text
        assert ("Parking fee: 0.00" in text) if language in ("en", "de") else ("停车费: 0.00" in text)


def test_preview_does_not_cut_a_program_rendered_amount():
    from aiops_diagnostics.dify.dify_console_publish import _trial_report_text

    text = "记录\n" * 1500 + "订单费用记录: 123456789012.12345678"
    assert (
        _trial_report_text({"result": {"report": {"callback_version": 6, "summary": text}}}, "completed")
        == text
    )


@pytest.mark.parametrize(
    "question",
    [
        "帮我看看这单是不是正常结束了",
        "这笔充电是否正常结束？",
        "Est-ce que ma recharge est terminée ?",
        "Is this charge finished?",
    ],
)
def test_knowledge_route_cannot_invent_order_facts_without_order_keywords(question):
    source = {
        "retrieved_reference_id": "ref-a",
        "title": "Help",
        "content": "Please open the application.",
        "media": [],
    }
    ctx = context(("knowledge", {"status": "found", "sources": [source]}))
    result = final_answer("这笔充电已正常结束，支付9.99元。", ctx, question=question)
    assert "9.99" not in str(result) and "正常结束" not in str(result)
    assert "Please open the application." in str(result)


@pytest.mark.parametrize(
    "content", ["使用 http://help.example.com 查看帮助。", "文档例子包含 https:// 不完整网址。"]
)
def test_unrenderable_source_does_not_discard_order_facts(content):
    source = {"retrieved_reference_id": "ref-a", "title": "Help", "content": content, "media": []}
    result = final_answer(
        "wrong", context(("billing", billing()), ("knowledge", {"status": "found", "sources": [source]}))
    )
    assert "4.62" in result["blocks"][0]["text"]
    assert content not in str(result)
    assert "未展示" in str(result)


def test_safety_and_official_support_do_not_depend_on_free_model_text():
    danger = final_answer("正常结束", {}, question="充电枪冒烟了")
    assert "远离设备" in str(danger) and "正常结束" not in str(danger)
    ctx = context(
        ("support", {"status": "found", "support": {"zh": {"label": "官方客服", "url": "tel:+12345678901"}}})
    )
    result = final_answer("已转接人工", ctx, question="联系人工")
    assert "tel:+12345678901" in str(result) and "已转接人工" not in str(result)


def test_composition_status_and_period_values_render_without_model_interpretation():
    result = billing()
    result["snapshot"]["order_summary"] = {
        "electricity": "1.086500",
        "time": "14",
        "createdTime": "2026-11-06 10:19:53",
        "stopTime": "2026-11-06 10:34:42",
    }
    result["snapshot"]["periods"] = {
        "status": "provided",
        "records": [
            {
                "startTime": "00:00",
                "endTime": "10:30",
                "electricFee": "1.47",
                "serviceFee": "0.73",
                "totalFee": "2.20",
                "electricity": "0.73",
                "feeRate": "3.0000",
            }
        ],
    }
    status = {
        "status": "found",
        "order_ids": [result["snapshot"]["order_no"]],
        "snapshot": {
            "order_no": result["snapshot"]["order_no"],
            "source": "cpms.chargingInfo",
            "status_role": "finished",
            "status_code": 1,
            "observed_at": "2026-11-12T00:00:00Z",
        },
    }
    text = final_answer("正常结束，合计8.00", context(("status", status), ("billing", result)))["blocks"][0][
        "text"
    ]
    assert "充电订单状态记录: 充电结束" in text
    assert "正常结束" not in text and "8.00" not in text
    assert all(v in text for v in ("1.086500", "3.0000", "2.20", "非独立计量", "来源"))


def test_order_list_and_long_result_keep_complete_amounts_and_mark_omission():
    result = billing()
    result["snapshot"]["periods"] = {
        "status": "partial",
        "records": [
            {
                "startTime": "00:00",
                "endTime": "23:59",
                "electricFee": "123456789012.12345678",
                "serviceFee": "0",
                "totalFee": "1",
                "feeRate": "2",
                "electricity": "3",
            }
        ]
        * 80,
    }
    ctx = context(
        (
            "orders",
            {
                "status": "found",
                "orders": [
                    {
                        "order_no": "TEST-CHARGE-001",
                        "created_time": "2026-11-12 00:00:00",
                        "station_name": "站点13800138000",
                    }
                ],
            },
        ),
        ("billing", result),
    )
    text = final_answer("wrong", ctx)["blocks"][0]["text"]
    assert "13800138000" not in text and "TEST-CHARGE-001" in text
    assert len(text) < 8000 and "展示长度限制" in text
    assert "123456789012.12345678" in text
    assert all(line.endswith("123456789012.12345678") for line in text.splitlines() if "123456" in line)
