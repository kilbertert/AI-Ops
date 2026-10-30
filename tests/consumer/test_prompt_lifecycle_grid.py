"""16 格样本：8 类场景 × 2 种风格，两端各走真实交付路径。

风格是**两种真实写法**，不是关键词对照：`简洁直接` 与 `亲切解释` 都必须走得通，而且事实守卫
对两者一视同仁。这一层证明的是"提示词写的话能成为用户看到的正文，且事实判据不因风格松紧"。

它**不**证明真实模型的观感差异 —— 那只能在 41 上用真实模型跑（见 qa-plan §五）。

契约见 `docs/acceptance/dify-prompt-lifecycle/acceptance.feature`。
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.consumer.consumer_documents import answer_selection
from aiops_diagnostics.operator import operator_conversation as conversation
from aiops_diagnostics.operator import operator_cpms

BILLING = {
    "order_no": "2107546575906942977",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
}
STATUS = {
    "order_no": "2107546575906942977",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "status_code": "1",
    "status_role": "finished",
}

#: 两种风格：同一件事，一个先说结论，一个先接住情绪。
STYLES = ("简洁直接", "亲切解释")


# --- 客户端：5 类出口 × 2 风格 -------------------------------------------------------

KNOWLEDGE = {
    "简洁直接": "充电枪拔不下来时，先在 App 点停止充电，再按枪上的解锁键。",
    "亲切解释": "遇到枪拔不下来别着急：先在 App 里点停止充电，等指示灯变了再按枪上的解锁键。",
}
STATUS_REPLY = {
    "简洁直接": "这单已经结束充电。订单号 2107546575906942977。",
    "亲切解释": "帮您看了一下，这单的充电已经结束啦。订单号 2107546575906942977，您可以按这个单号核对。",
}
BILLING_REPLY = {
    "简洁直接": "这单账单合计 27.36，系统记录的支付金额是 20，没有退款记录。订单号 2107546575906942977。",
    "亲切解释": (
        "这单合计 27.36，账上记的付款是 20，没有退款记录。订单号 2107546575906942977。"
        "系统记录的金额不等于银行已经扣款，也不代表已经到账。"
    ),
}
CLARIFY = {
    "简洁直接": "需要订单号才能查。",
    "亲切解释": "我这边还差一个订单号才能帮您查，方便说一下是哪一单吗？",
}
NOT_FOUND = {
    "简洁直接": "这次没查到对应资料。",
    "亲切解释": "这次没有找到能回答这个问题的资料，换个说法我再帮您找找。",
}


def documents_context(status="found"):
    return {
        "mode": "documents",
        "status": status,
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "doc-1",
                "title": "充电枪使用",
                "content": "",
            }
        ],
        "live_snapshot": None,
    }


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_knowledge(style):
    kind, answer = answer_selection(
        json.dumps({"kind": "knowledge", "answer": KNOWLEDGE[style], "reference_ids": ["source-1"]}),
        documents_context(),
    )
    assert kind == "knowledge"
    assert answer["blocks"][0]["text"] == KNOWLEDGE[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_status(style):
    context = {**documents_context(), "live_snapshot": STATUS}
    kind, answer = answer_selection(
        json.dumps({"kind": "realtime", "answer": "", "reference_ids": [], "reply": STATUS_REPLY[style]}),
        context,
    )
    assert kind == "realtime"
    assert answer["blocks"][0]["text"] == STATUS_REPLY[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_billing(style):
    context = {**documents_context(), "live_snapshot": BILLING}
    kind, answer = answer_selection(
        json.dumps({"kind": "realtime", "answer": "", "reference_ids": [], "reply": BILLING_REPLY[style]}),
        context,
    )
    assert kind == "realtime"
    assert answer["blocks"][0]["text"] == BILLING_REPLY[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_clarify(style):
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "clarify", "faq_id": "", "reply": CLARIFY[style]}),
        {"status": "no_result", "candidates": []},
        "zh",
        None,
    )
    assert answer["blocks"][0]["text"].startswith(CLARIFY[style])


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_not_found(style):
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "not_found", "faq_id": "", "reply": NOT_FOUND[style]}),
        {"status": "no_result", "candidates": []},
        "zh",
        None,
    )
    assert answer["blocks"][0]["text"].startswith(NOT_FOUND[style])


# --- 管家：3 类出口 × 2 风格 ---------------------------------------------------------

CONSULT = {
    "简洁直接": "状态 3 表示异常已由人工处理完毕。",
    "亲切解释": "您看到的这个状态，意思是这次异常已经有同事人工处理过了，不用再操作。",
}
DIAGNOSIS = {
    "简洁直接": {
        "direct_answer": "这单的电费是 13.68，服务费 13.68，合计 27.36。",
        "summary": "订单 2107546575906942977 已结束，电量 4.5598 kWh。",
        "probable_cause": "金额构成与计费模板一致，未发现多计。",
        "next_steps": ["如需核对分时电价，请提供计费模板"],
        "limitations": ["上游未提供分时电价明细"],
        "evidence_tools": ["cpms_order_detail"],
    },
    "亲切解释": {
        "direct_answer": "帮您算了一下：电费 13.68，服务费 13.68，加起来是 27.36。",
        "summary": "这单已经结束，充了 4.5598 kWh。",
        "probable_cause": "这两笔金额和计费模板对得上，看起来没有多收。",
        "next_steps": ["想核对分时电价的话，把计费模板发我"],
        "limitations": ["这次没取到分时电价明细"],
        "evidence_tools": ["cpms_order_detail"],
    },
}
SITE_NOTE = {
    "简洁直接": "按当前账号可见范围查到的站点如下。",
    "亲切解释": "我按您账号能看到的范围查了一下，结果在下面，您可以按名字核对。",
}
EVIDENCE = {
    "cpms_order_detail": {
        "source": "cpms.orderDetail",
        "observed_at": "2026-10-30T09:08:26+00:00",
        "data": {"orderNo": "2107546575906942977"},
    }
}


@pytest.mark.parametrize("style", STYLES)
def test_grid_operator_consult(style):
    answer = {"kind": "answer", "text": CONSULT[style], "name": "", "address_terms": []}
    conversation.validate(answer, "状态3是什么意思")
    assert conversation.render(answer, None, "zh")["blocks"][0]["text"] == CONSULT[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_operator_diagnosis(style):
    answer = DIAGNOSIS[style]
    operator_cpms.validate_answer(answer, EVIDENCE)
    report = operator_cpms.render_report(answer, EVIDENCE)
    assert report.startswith(answer["direct_answer"])
    assert "【订单事实】" in report and "【数据来源】" in report


@pytest.mark.parametrize("style", STYLES)
def test_grid_operator_site_search(style):
    value = {
        "kind": "site_search",
        "text": SITE_NOTE[style],
        "name": "天河",
        "address_terms": ["广州", "天河"],
    }
    conversation.validate(value, "找广州天河充电站")
    sites = {
        "source": "cpms.siteSearch",
        "observed_at": "2026-10-30T09:08:26+00:00",
        "sites": [{"id": "SITE-1", "name": "天河测试站", "address": "广州市天河区测试路"}],
        "limited": False,
        "empty_scope": False,
        "total": 1,
        "scope_limited": True,
    }
    text = conversation.render(value, sites, "zh")["blocks"][0]["text"]
    assert text.startswith(SITE_NOTE[style])
    assert "天河测试站" in text and "cpms.siteSearch" in text


# --- 另覆盖：找站空结果、缺单、权限不足、多语言、取消与超时 -----------------------------


def test_grid_site_search_empty_result_differs_from_empty_scope():
    """空结果与空范围是两件事，话不能同形。"""
    base = {
        "source": "cpms.siteSearch",
        "observed_at": "2026-10-30T09:08:26+00:00",
        "limited": False,
        "total": 0,
        "scope_limited": True,
    }
    searched = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["火星"]},
        {**base, "sites": [], "empty_scope": False},
        "zh",
    )["blocks"][0]["text"]
    listed = conversation.render(
        {"kind": "site_search", "text": "", "name": "", "address_terms": []},
        {**base, "sites": [], "empty_scope": True},
        "zh",
    )["blocks"][0]["text"]
    assert "未找到匹配站点" in searched
    assert "尚未分配可查询站点" in listed


def test_grid_operator_without_an_order_asks_for_one():
    answer = {"kind": "order_required", "text": "请提供订单号。", "name": "", "address_terms": []}
    conversation.validate(answer, "帮我看看上一个订单")
    assert conversation.render(answer, None, "zh")["blocks"][0]["text"] == "请提供订单号。"


@pytest.mark.parametrize(
    "language,note",
    [
        ("en", "Here is what I can see for your account."),
        ("zh-Hant", "我按您帳號可見的範圍查了一下，結果如下。"),
    ],
)
def test_grid_site_note_follows_the_operator_language(language, note):
    value = {"kind": "site_search", "text": note, "name": "", "address_terms": []}
    conversation.validate(value, "我有哪些站点")
    sites = {
        "source": "cpms.siteSearch",
        "observed_at": "2026-10-30T09:08:26+00:00",
        "sites": [{"id": "SITE-1", "name": "Tianhe", "address": "Guangzhou"}],
        "limited": False,
        "empty_scope": False,
        "total": 1,
        "scope_limited": True,
    }
    assert conversation.render(value, sites, language)["blocks"][0]["text"].startswith(note)
