"""新版事实段的正例、边界和多语言必须一起验证。"""

import pytest

from aiops_diagnostics.consumer.consumer_facts import check_status_prose, facts_for
from aiops_diagnostics.consumer.consumer_language import COPY
from aiops_diagnostics.consumer.consumer_reply import model_reply, validate_reply
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T00:00:00Z",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
    "status_code": "1",
    "status_role": "finished",
}


@pytest.mark.parametrize("language", ["fr", "de", "th"])
def test_segments_use_existing_language_and_evidence(language):
    facts = facts_for(SNAPSHOT, language)
    assert COPY[language]["finished"] in facts["status_line"]
    answer = model_reply("{{fact:billing_line}}", SNAPSHOT, language=language, contract="slots")
    text = answer["blocks"][0]["text"]
    for value in ("123456789012345", "27.36", "20", COPY[language]["source_billing"]):
        assert value in text
    assert "CNY" not in text


@pytest.mark.parametrize(
    "text",
    [
        "合计27.36，付款20，退款0。订单123456789012345。",
        "这单并非{{fact:status_line}}",
        "每小时{{fact:billing_line}}元",
        "{{fact:order_line}}",
    ],
)
def test_new_contract_requires_complete_standalone_fact_segments(text):
    with pytest.raises(ChatRunUnavailable):
        validate_reply(text, SNAPSHOT, language="zh", contract="slots")


def test_style_surrounds_a_fact_segment_without_rewriting_it():
    text = "我帮你核对了一下。\n{{fact:billing_line}}\n可以再告诉我你想了解哪一项。"
    answer = model_reply(text, SNAPSHOT, language="zh", contract="slots")
    assert answer["blocks"][0]["text"].startswith("我帮你核对了一下。")
    assert "27.36" in answer["blocks"][0]["text"]


@pytest.mark.parametrize(
    "text",
    [
        "这笔订单没有结束，还在充电。",
        "这单未结束。",
        "The order has not finished and is still charging.",
        "Der Ladevorgang ist nicht abgeschlossen.",
        "La recharge n'est pas terminée.",
        "ยังไม่เสร็จสิ้น",
    ],
)
def test_free_prose_cannot_reverse_the_status_segment(text):
    with pytest.raises(ChatRunUnavailable):
        validate_reply(text + "\n{{fact:billing_line}}", SNAPSHOT, language="zh", contract="slots")


def test_live_segments_survive_readback_and_reject_changed_cached_text(tmp_path, monkeypatch):
    import json
    import sqlite3

    from execution.test_app_runtime_dify_entry import dify_chat
    from execution.test_app_runtime_node_entry import ORDER_A

    from aiops_diagnostics.consumer import consumer_chat_service
    from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
    from consumer.test_consumer_live_context import Flow, ask, done, wire

    class SegmentFlow(Flow):
        def run(self, **kwargs):
            response = super().run(**kwargs)
            body = {
                "kind": "realtime",
                "answer": "",
                "reference_ids": [],
                "reply": "我帮你确认了一下。\n{{fact:status_line}}",
            }
            return ChatflowReply(
                response.workflow_run_id,
                response.task_id,
                response.conversation_id,
                response.message_id,
                json.dumps(body),
                {},
            )

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0]["callback_version"] = 5
        env["bindings"].write_text(json.dumps(doc))
        monkeypatch.setattr(consumer_chat_service, "DifyChatflowClient", SegmentFlow)
        qa = ask(env).json()["qa_id"]
        final = done(env, qa)
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text
        assert ORDER_A in final.json()["result"]["blocks"][0]["text"]
        assert done(env, qa).json()["result"] == final.json()["result"]
        run = runtime.dify_run_store.by_qa(qa)
        tampered = final.json()["result"]
        tampered["blocks"][0]["text"] = "不是这次查询结果"
        with sqlite3.connect(env["db"]) as db:
            db.execute(
                "UPDATE assistant_questions SET result_json=? WHERE qa_id=?", (json.dumps(tampered), qa)
            )
            db.execute(
                "UPDATE chat_dify_runs SET snapshot_json=? WHERE run_id=?", (json.dumps(tampered), run.run_id)
            )
        assert done(env, qa).status_code != 200


# --- 第四轮复核的四个实际反例（先失败，再修） ---------------------------------


#: 状态轮的快照形状（`cpms.chargingInfo`）：没有金额字段，只有状态与时间。
STATUS_SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T00:00:00Z",
    "status_code": "1",
    "status_role": "finished",
}


def test_a_negated_state_prose_cannot_contradict_the_evidence():
    """A. 快照说结束，旁白写"尚未完结，仍在供电"必须拒绝。

    判据不是"再列几个同义词"，而是**否定词 × 状态域名词**：旁白是否定或改写状态时，
    无论用哪个词形，都在这里的两个维度上撞到。
    """
    text = "这笔订单尚未完结，仍在供电。\n{{fact:status_line}}"
    with pytest.raises(ChatRunUnavailable):
        model_reply(text, STATUS_SNAPSHOT, language="zh", contract="slots")


def test_b_a_normal_english_follow_up_survives():
    """B. 英文正常追问不属于状态断言，必须保留。"""
    text = "{{fact:status_line}}\nDo you have any other questions about charging?"
    answer = model_reply(text, STATUS_SNAPSHOT, language="en", contract="slots")
    assert "other questions" in answer["blocks"][0]["text"]


def test_d_an_inline_prefix_on_a_fact_segment_is_refused():
    """D. `账单内容：{{fact:billing_line}}` 违反独立行协议，必须拒绝。

    生成契约（协议块）与消费契约必须一致：片段单独占一行、不得加前后缀。
    """
    from aiops_diagnostics.consumer.consumer_documents import _protocol_block

    assert "每个片段单独占一行，不得添加前后缀" in _protocol_block()
    with pytest.raises(ChatRunUnavailable):
        model_reply("账单内容：{{fact:billing_line}}", SNAPSHOT, language="zh", contract="slots")


#: 真实模型样本里出现、被守卫**误拒**的正常追问（2026-10-31，32 次样本第 2 轮）。
#: 两者的共同点：含否定字或状态词，但整句都不是状态断言。
_NORMAL_FOLLOW_UPS = [
    "还需要对照原账单和支付流水才能判断有没有异常。",
    "你是不是想核对订单对应的充电站？",
    "可以再给我一点线索，比如城市、路段或订单上的站点名称。",
    "这笔单的费用明细要看充电记录里的哪一项？",
]


@pytest.mark.parametrize("text", _NORMAL_FOLLOW_UPS)
def test_e_a_normal_follow_up_with_a_question_form_is_not_a_status_assertion(text):
    """E. A-不-A 疑问式与名词化的场所词不算状态断言。

    上一版用「否定词 × 状态词」单维判据：`有没有异常` 里的 `没有`+`异常`、
    `充电站` 里的 `充电` 都被当成改写状态，把正常追问整轮拒掉。
    修法是**先剥问句形式和名词**再判，而不是删掉判据 —— 真否定仍要拦（见下一条）。
    """
    check_status_prose(text)


@pytest.mark.parametrize(
    "text",
    [
        "这笔订单尚未完结，仍在供电。",
        "订单没有结束。",
        "订单已经结束了。",
        "The order has not finished and is still charging.",
    ],
)
def test_e_a_real_negation_or_assertion_is_still_refused(text):
    """对照组：同一批放宽之后，真正的状态改写必须仍然被拒。

    没有这一条，上面的修法就退化成"把判据放松到什么都不拦"。
    """
    with pytest.raises(ChatRunUnavailable):
        check_status_prose(text)


def test_non_json_answer_reports_shape_without_leaking_text():
    """`invalid document answer` 要带上**形态**，但不能把模型原文写进错误里。

    2026-11-01 排查订单链路时只拿到 `invalid document answer`，无法区分
    "模型没按 JSON 答" 与 "答了 JSON 但被 markdown 围栏包住"。
    原文是用户可见文本（可能含订单号、金额），所以只描述形态。
    """
    from aiops_diagnostics.consumer.consumer_documents import _json_failure_shape

    assert _json_failure_shape("   ") == "empty"
    assert _json_failure_shape("```json\n{}\n```") == "fenced"
    assert _json_failure_shape('{"kind":"realtime",') == "json_syntax"
    assert _json_failure_shape("这单已经充电结束了。").startswith("plain_text:")
    # 形态里**不含**原文
    assert "充电" not in _json_failure_shape("这单已经充电结束了。")


def test_a_complete_selection_followed_by_stray_punctuation_is_still_used():
    """对象完整、只是尾巴多了 `"}"，不该判成坏答复。

    2026-11-01 生产实测：同一版提示词 12 条答复里 4 条是这个形状，对象本身字段全对。
    整轮丢掉它们，抹掉的是**已经成功取到数**的那一次。
    """
    from aiops_diagnostics.consumer.consumer_documents import load_selection

    tail_only = (
        '{"kind":"realtime","answer":"","reference_ids":[],'
        '"reply":"{{fact:status_line}}\\n{{fact:observed_line}}"}"}'
    )
    assert load_selection(tail_only)["kind"] == "realtime"
    assert load_selection('{"kind":"realtime","reference_ids":[]}}')["kind"] == "realtime"
    # 对象里的花括号（事实片段）不参与"到哪儿结束"的判断
    nested = '{"kind":"knowledge","answer":"a{b}c","reference_ids":["s1"]}'
    assert load_selection(nested)["answer"] == "a{b}c"


def test_tolerance_does_not_excuse_an_answer_that_is_not_a_json_object():
    """放宽的边界：只救"对象 + 标点尾巴"。

    前面带话、后面带句子、谱了一半的对象，都不是格式手滑，是没按协议答 ——
    这些仍然整轮拒绝，否则"把正文当 JSON 交上来"会变成可以接受的行为。
    """
    import pytest

    from aiops_diagnostics.consumer.consumer_documents import load_selection
    from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

    for raw in (
        '{"kind":"x"} 好的',
        '好的{"kind":"x"}',
        '{"kind":"realtime","reference_ids":[',
        "这单已经充电结束了。",
        "",
    ):
        with pytest.raises(ChatRunUnavailable) as caught:
            load_selection(raw)
        assert caught.value.code == "CHATFLOW_ANSWER_INVALID"


def test_every_reader_of_the_model_output_tolerates_the_same_tail():
    """同一份输出有三个读取点，容忍度必须一致。

    只放开 `answer_selection`、另外两处仍严格，会让"答出来了"变成"存不下正文"
    或"正文退回固定文案" —— 同一个成因换个位置失败。2026-11-01 就是这样漏的：
    先只想着解析，`store_reply_source` 与 `_has_reply` 都还在严格解析。
    """
    from aiops_diagnostics.consumer.consumer_knowledge import _has_reply

    tail_only = (
        '{"kind":"realtime","answer":"","reference_ids":[],'
        '"reply":"{{fact:status_line}}\\n{{fact:observed_line}}"}"}'
    )
    assert _has_reply(tail_only) is True
    assert _has_reply("这单已经充电结束了。") is False


def test_protocol_says_to_use_only_the_segments_this_turn_actually_has():
    """状态查询引用了 `billing_line` 是**合法 JSON 里的错内容**，守卫拒绝正确。

    2026-11-01 生产实测：9 次订单查询里 3 次的模型输出是这个形状 ——
    账单段并不在本轮快照里，模型却照"账单必须引用billing_line"那句写上了它。
    协议块要讲清"只用本轮给出的段"，否则模型只能靠猜。
    """
    from aiops_diagnostics.consumer.consumer_documents import _protocol_block

    text = _protocol_block()
    assert "只引用本轮实际给出的段" in text
    assert "看不到billing_line就不要写它" in text
