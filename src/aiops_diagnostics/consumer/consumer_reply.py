"""让提示词写正文，事实由系统填：片段契约 + 旧契约兼容。

产品诉求是"在编排页改提示词就能改说话方式"。真正难的不是让模型写字，而是**让写出来的话
不能造假**。第一版用"数字必须能在证据里找到"来兜，复核用反例证明它两头都错（见
`consumer_facts` 的模块文档）。现在的形状是：

* 模型写 ``合计{{fact:total_amount}}``，系统用本轮快照填 ``27.36``；
* 模型自己的文字里不许出现数字、状态名或支付结果 —— 这三样是**有限集合**，不是语义猜测；
* 其余措辞完全自由。

老应用（`callback_version < 5`）继续走三条弱判据，行为不变。代价写在 `consumer_facts`。
"""

from __future__ import annotations

from aiops_diagnostics.consumer import consumer_facts
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

#: 正文上限。答复是几句话，不是文档；放宽会让一次跑飞的模型把整屏刷满。
MAX_REPLY_CHARS = 2000

#: 澄清／无结果／转人工的上限。它们是一句话的通知。
MAX_NOTICE_CHARS = 600

#: 不许出现在用户正文里的内部标识。`cpms.` 是接口名，`status_code=` 是原始码。
FORBIDDEN = ("cpms.", "status_code=", "status_role=")


def validate_reply(reply: object, snapshot: dict, *, language: str, contract: str = "slots") -> str:
    """校验模型写的正文，返回可直接交付的文本。不合格就整轮失败。

    **默认是片段契约**：判据的默认值必须是强的那个，弱判据只能由运行期显式传入
    （旧发布版本的绑定会传 ``contract="legacy"``）。反过来（默认弱、新契约显式开）会让
    任何忘记传参的调用点悄悄退回弱判据 —— 复核正是这样漏掉标签互换的。
    """
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is empty")
    text = reply.strip()
    if len(text) > MAX_REPLY_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "reply is too long")
    if contract == "slots":
        return consumer_facts.check_bound_reply(text, snapshot, language)
    consumer_facts.check_legacy(text, snapshot, language)
    return text


def model_reply(reply: object, snapshot: dict, *, language: str, contract: str = "slots") -> dict:
    """把模型写的正文包成 blocks-v1。前端只认 `blocks[]`，形状必须与既有答复一致。"""
    text = validate_reply(reply, snapshot, language=language, contract=contract)
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}


def validate_notice(reply: object, *, language: str) -> str:
    """校验提示词为**澄清／无结果／转人工**写的正文。

    这三类答复没有任何本轮证据：没有快照、没有资料片段。所以判据是"一个数字都不许出现"，
    不许写链接，不许有支付结果或"已提交"类执行声明。没有带正文时行为与之前完全一致。
    """
    if not isinstance(reply, str) or not reply.strip():
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is empty")
    text = reply.strip()
    if len(text) > MAX_NOTICE_CHARS:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice is too long")
    for token in FORBIDDEN:
        if token in text:
            raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "notice exposes internal identifiers")
    consumer_facts.check_notice(text)
    return text
