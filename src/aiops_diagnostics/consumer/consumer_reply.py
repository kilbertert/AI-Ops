"""让 chatflow 的提示词写正文，同时不许它编数字。

2026-10-30 产品诉求：**在 Dify 编排页改提示词就能调整客户端的说话方式**。

此前做不到，原因不是提示词不生效，而是正文被丢掉了：`answer` 节点按 `{kind, faq_id}`
严格解析，模型写的字只用来做分类；用户看到的正文由 `consumer_plain` 按快照渲染。
于是"改提示词让回复更拟人"这件事在 C 端**没有任何着力点**。

这个模块给模型开一个写正文的口子：`answer` 节点可以在 JSON 里多带一个 `reply` 字段。
网关**不信任**它，只做三件事：

1. **数字必须来自本轮快照**：快照里每一个非空金额都必须在正文里出现（数值相等即可，
   `2.4` 与 `2.40` 都算），否则拒收 —— 编造或漏掉金额都会在这里被拦下。
2. **内部标识不进正文**：`cpms.*`、`status_code=`、`status_role=` 一律拒收。
3. **订单号必须在正文里**：没有它，读者无法核对这是哪一单。

拒收不是"降级成模板"而是**整轮失败**：一份混着假数字的答复比一句"服务暂时不可用"更糟。
没带 `reply` 时行为与之前完全一致（用确定性渲染），所以老应用不会被这次改动影响。
"""

from __future__ import annotations

import re

from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 正文上限。答复是几句话，不是文档；放宽会让一次跑飞的模型把整屏刷满。
MAX_REPLY_CHARS = 2000

#: 不许出现在用户正文里的内部标识。`cpms.` 是接口名，`status_code=` 是原始码。
FORBIDDEN = ("cpms.", "status_code=", "status_role=")

#: 快照里"读者要能核对"的金额字段。空值不要求出现 —— 没给的值不能凭空写。
AMOUNT_FIELDS = ("order_amount", "recorded_payment_amount", "recorded_refund_amount")

#: 澄清／无结果／转人工这三类正文的上限。它们是**一句话的通知**，不是答复正文。
MAX_NOTICE_CHARS = 600

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    found: set[float] = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(float(token))
        except ValueError:  # pragma: no cover - 正则保证可解析
            continue
    return found


def validate_reply(reply: object, snapshot: dict, *, language: str) -> str:
    """校验模型写的正文，返回可直接交付的文本。不合格就整轮失败。"""
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is empty")
    text = reply.strip()
    if len(text) > MAX_REPLY_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is too long")
    for token in FORBIDDEN:
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply exposes internal identifiers")
    order_no = str(snapshot.get("order_no") or "")
    if order_no and order_no not in text:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply omits the order number")
    # 金额：快照里给了的，正文里必须出现且数值相等。这是"不许漏写"的判据。
    present = _numbers(text)
    for field in AMOUNT_FIELDS:
        value = snapshot.get(field)
        if value is None or str(value).strip() == "":
            continue
        try:
            expected = float(str(value))
        except ValueError:
            continue
        if expected not in present:
            # 金额为 0 时，"没有这一项"就是它的说法：``没有退款记录`` 已经说完了这个事实，
            # 再要求写出 ``0`` 只会逼模型说一句不自然的话。
            from aiops_diagnostics.consumer.consumer_facts import covers_zero

            if expected == 0 and covers_zero(text, field):
                continue
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", f"reply does not carry the recorded {field}")
    # 反过来：正文里的**每一个**数字都必须能在证据里找到出处。
    # 只要求"该有的都在"拦不住编造 —— 一段既有 2.40 又编了 2.5 的正文会照样通过。
    # 事实的四条判据（标签绑定／货币断言／到账承诺／状态方向）。它们问的不是"数字对不对"，
    # 而是"这句话把哪个值说成了什么" —— 数字集合比对对"合计 20，付款 27.36"无能为力。
    from aiops_diagnostics.consumer import consumer_facts

    consumer_facts.check(text, snapshot)
    allowed = _evidence_numbers(snapshot, language)
    # 带货币单位的数字不享受"小计数"豁免：``每小时 3 元`` 是价格断言，``第 3 条`` 不是。
    allowed -= consumer_facts.currency_claims(text)
    invented = sorted(present - allowed)
    if invented:
        raise ChatRunUnavailable(
            "CHATFLOW_ANSWER_INVALID", "reply contains numbers that are not in the evidence"
        )
    return text


def _evidence_numbers(snapshot: dict, language: str) -> set[float]:
    """证据里出现过的所有数字：快照各字段（含订单号）＋可核对的时间。

    另外放行 0–10 的整数：它们是"第 2 条""3 步"这类计数，不构成对金额的断言，
    卡住它们只会让模型不敢组织语言。金额与订单号不在此列 —— 它们必须逐字来自快照。
    """
    import json

    from aiops_diagnostics.consumer.consumer_time import readable_time

    corpus = json.dumps(snapshot, ensure_ascii=False, default=str) + " "
    corpus += readable_time(snapshot.get("observed_at"))
    allowed = _numbers(corpus)
    allowed.update(float(n) for n in range(0, 11))
    return allowed


def model_reply(reply: object, snapshot: dict, *, language: str) -> dict:
    """把模型写的正文包成 blocks-v1。前端只认 `blocks[]`，形状必须与既有答复一致。"""
    text = validate_reply(reply, snapshot, language=language)
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


def validate_notice(reply: object, *, language: str) -> str:
    """校验提示词为**澄清／无结果／转人工**写的正文。

    这三类答复没有任何本轮证据：没有快照、没有资料片段。所以判据比 ``validate_reply`` 更硬——
    **一个数字都不许出现**。理由很直接：这类话里出现的数字只可能是金额、时限、单号或承诺
    （"3 个工作日内"），而这三类答复手上什么都没有。链接也不许写：客服入口由系统按内容包
    追加，让模型复述 URL 只会多一个出错的地方。

    没有带正文时行为与之前完全一致（系统固定文案），所以老应用不受影响。
    """
    from aiops_diagnostics.consumer import consumer_facts

    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is empty")
    text = reply.strip()
    if len(text) > MAX_NOTICE_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is too long")
    for token in FORBIDDEN:
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice exposes internal identifiers")
    if _NUMBER.search(text):
        raise ChatRunUnavailable(
            "CHATFLOW_FACT_INVENTED_AMOUNT", "notice states a number without any evidence this turn"
        )
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        raise ChatRunUnavailable(
            "CHATFLOW_ANSWER_INVALID", "notice must not write links; the support entry is appended"
        )
    if consumer_facts.EXECUTED.search(text):
        raise ChatRunUnavailable("CHATFLOW_FACT_PROMISE", "notice claims an action the system did not take")
    consumer_facts.check_promises(text)
    return text
