"""用户恢复自然语言交付：正文、引用和原入口一致，授权与敏感信息校验不退回。"""

import pytest

from aiops_diagnostics.consumer.consumer_agent import final_answer
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable


def test_natural_summary_is_delivered_with_sources_instead_of_raw_excerpts():
    source = {
        "retrieved_reference_id": "r1",
        "title": "操作指南",
        "content": "先打开App，再检查状态。",
        "media": [],
    }
    context = {"calls": [{"tool": "knowledge", "result": {"status": "found", "sources": [source]}}]}
    answer = final_answer("请先打开App查看订单状态，再告诉我当前提示。", context)
    assert answer["blocks"][0]["text"] == "请先打开App查看订单状态，再告诉我当前提示。"
    assert any(b.get("reference_id") == "r1" for b in answer["blocks"])
    assert "原文摘录" not in answer["blocks"][0]["text"]


@pytest.mark.parametrize("raw", ["", " " * 3, "x" * 8001, None])
def test_natural_answer_still_requires_a_valid_bounded_body(raw):
    with pytest.raises(ChatRunUnavailable, match="有效正文"):
        final_answer(raw, {})


def test_pending_tools_and_unverified_links_still_refuse_delivery():
    with pytest.raises(ChatRunUnavailable, match="工具记录尚未完成"):
        final_answer("正在查", {"calls": [{"tool": "billing", "result": None}]})
    with pytest.raises(ChatRunUnavailable, match="link was not"):
        final_answer("点击 https://unverified.example.com 支付", {"calls": []})


def test_preview_keeps_complete_natural_answer():
    from aiops_diagnostics.dify.dify_console_publish import _trial_report_text

    text = "来源记录说明\n" * 500 + "订单费用记录4.62，支付金额记录4.71。"
    assert (
        _trial_report_text({"result": {"report": {"callback_version": 6, "summary": text}}}, "completed")
        == text
    )


def test_existing_danger_guidance_and_official_contact_remain_available():
    assert "远离设备" in str(final_answer("正常充电", {}, question="充电枪冒烟了"))
    routes = {
        "zh": {"label": "官方客服", "url": "tel:+8612345678901"},
        "en": {"label": "Support", "url": "tel:+441234567890"},
    }
    context = {"calls": [{"tool": "support", "result": {"status": "found", "support": routes}}]}
    result = final_answer("请联系官方客服，我没有代为转接。", context, language="zh")
    assert "tel:+8612345678901" in result["blocks"][-1]["text"]
    assert "+441234567890" not in str(result)


@pytest.mark.parametrize("status", ["unavailable", "limited", "refused"])
def test_natural_answer_keeps_knowledge_outage_distinct_from_empty_search(status):
    ctx = {"calls": [{"tool": "knowledge", "result": {"status": status}}]}
    answer = final_answer("资料暂时不可用，请稍后再试。", ctx)
    assert answer["retrieval_status"] == "unavailable"
    source = {"retrieved_reference_id": "r1", "title": "指南", "content": "查看订单。", "media": []}
    ctx["calls"].append({"tool": "knowledge", "result": {"status": "found", "sources": [source]}})
    assert final_answer("本次查到一份指南。", ctx)["retrieval_status"] == "found"


def test_official_support_https_url_is_allowed_but_invented_url_is_not():
    url = "https://help.example.com/contact"
    ctx = {
        "calls": [
            {
                "tool": "support",
                "result": {
                    "status": "found",
                    "support": {"zh": {"label": "官方客服", "url": url}},
                },
            }
        ]
    }
    assert url in final_answer("请使用官方入口 " + url, ctx)["blocks"][0]["text"]
    with pytest.raises(ChatRunUnavailable):
        final_answer("请使用 https://other.example.com/contact", ctx)
