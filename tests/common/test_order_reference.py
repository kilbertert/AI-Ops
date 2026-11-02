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

    判据只有一份才算修好：两处的源码里都不该再有自己那套「有没有『订单』」的正则。
    """
    import inspect

    from aiops_diagnostics.dify import dify_console_publish
    from aiops_diagnostics.gateway import gateway_api

    assert "names_order" in inspect.getsource(gateway_api)
    assert "order_reference" in inspect.getsource(dify_console_publish._native_order)
    # 预览桥用的是同一份：订单句子但没有单号时返回空串，由下游澄清。
    assert dify_console_publish._native_order("我上一个订单是什么情况") == ""
