"""「订单」二字不是门槛：只写了单号的句子同样在指一张订单。"""

import pytest

from aiops_diagnostics.common.order_reference import names_order


@pytest.mark.parametrize(
    "question",
    [
        "帮我分析这笔订单的电费2116050516255272961",
        # 2026-11-02 真实截图：没有「订单」二字，只有「这笔单」。
        "帮我分析这笔单的电费2116050516255272961",
        "帮我分析这单的电费2116050516255272961",
        "分析一下2116050516255272961这笔",
        "帮我看看这笔充电的电费 2116050516255272961",
        # 整条消息就是单号（用户直接粘贴）。
        "2116050516255272961",
        # 说了「订单」但没有单号：仍然按订单语境处理，由下游决定要不要澄清。
        "我上一个订单是什么情况",
    ],
)
def test_a_bare_order_number_names_an_order(question):
    assert names_order(question)


@pytest.mark.parametrize(
    "question",
    [
        "帮我找一下广州天河附近的充电站",
        # 站点名里的日期与时刻：都是被 `-`/`:` 切开的短数字段，凑不够单号长度。
        "帮我查询充电站 TEST2026-10-29 03:00:09",
        "帮我查一下 2026-11-02 03:00 之后的充电站",
        "我的手机号是13800001111，帮我查一下",
        "价格是50元",
        "你好",
        "",
    ],
)
def test_other_numbers_are_not_orders(question):
    assert not names_order(question)


def test_both_entries_ask_this_one_predicate():
    """正式入口与预览桥各写过一份判据，于是同一句话走试用能识别、走正式入口不能。

    判据与候选集都只有一份才算修好：两处的源码里都不该再有自己那套订单正则。
    """
    import inspect

    from aiops_diagnostics.dify import dify_console_publish
    from aiops_diagnostics.gateway import gateway_api

    assert "order_candidates" in inspect.getsource(gateway_api)
    assert "order_candidates" in inspect.getsource(dify_console_publish._native_order)
    # 预览桥用的是同一份：订单句子但没有单号时返回空串，由下游澄清。
    assert dify_console_publish._native_order("我上一个订单是什么情况") == ""


def test_digit_runs_is_the_candidate_set_both_the_gate_and_the_extractor_use():
    """门槛说「有长数字段」时，提取必须取**那一段**，不是第一个含数字的 token。

    2026-11-02 评审实测：站点名 `TEST2026-10-29` 排在真正单号前面时，旧写法放行之后
    从左边先抓日期，于是「查这张单」变成「查这个日期」。
    """
    from aiops_diagnostics.common.order_reference import digit_runs

    question = "帮我分析站点 TEST2026-10-29 这笔单2116050516255272961"
    assert names_order(question)
    assert digit_runs(question) == ["2116050516255272961"]
    assert digit_runs("帮我查询充电站 TEST2026-10-29 03:00:09") == []


# --------------------------------------------------------------- 2026-11-02 交接复现


@pytest.mark.parametrize(
    "question,expected",
    [
        # Codex 在 099c1207 实测：带字母前缀的完整标识符被截成了纯数字尾巴。
        ("订单 A2116050516255272961", "A2116050516255272961"),
        ("帮我查订单 A2116050516255272961", "A2116050516255272961"),
    ],
)
def test_a_letter_prefixed_identifier_keeps_its_prefix(question, expected):
    """标识符是一个整体，不能只取它里面那段数字。

    单号一旦带字母前缀，前缀就是它的一部分：截掉之后去查的是**另一张单**，
    而且查不到时系统的说法是「订单不存在」——看起来像用户写错了。
    """
    from aiops_diagnostics.common.order_reference import order_candidates

    assert order_candidates(question) == [expected]


def test_two_different_orders_in_one_message_are_not_silently_reduced_to_one():
    """一句话里点两张**不同**的单，不能悄悄只查一张。

    `订单 ORDER-1001 和订单2116050516255272961` 里的短数字段（`-1001` 的四位）
    不满足裸数字门槛，但它前面写着「订单」，那就是一张被点名的单。旧写法按候选集
    大小挑，结果是**默认选了后者**——用户以为问了 A 和 B，系统只答了 B。
    """
    from aiops_diagnostics.common.order_reference import order_candidates

    assert set(order_candidates("订单 ORDER-1001 和订单2116050516255272961")) == {
        "ORDER-1001",
        "2116050516255272961",
    }


@pytest.mark.parametrize(
    "question",
    [
        # 同一张单重复出现（显式写一次 + 裸写一次）仍是**一张**单。
        "订单2116050516255272961 这张单2116050516255272961",
        "帮我查订单 2116050516255272961",
        "帮我分析这笔单的电费2116050516255272961",
    ],
)
def test_the_same_order_repeated_is_one_order(question):
    from aiops_diagnostics.common.order_reference import order_candidates

    assert order_candidates(question) == ["2116050516255272961"]
