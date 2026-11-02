"""这条消息是不是在指某一张订单 —— 网关与预览桥共用的那一份判据。

提到订单有两条路：写出「订单」二字，或者**直接把单号写进句子**。第二条是运营与客服的
日常说法：「帮我分析这笔单的电费2116050516255272961」里一个「订单」都没有，只有号码。

这里曾经用「消息里有没有『订单』」当门槛，于是「这笔单」「这单」「这一笔」全部落进无
订单回合，模型只能回来请用户先选订单（2026-11-02 真实截图，管家试用与正式入口同形）。
门槛本来要挡的是**别的东西里的数字**：站点名 `TEST2026-10-29`、时刻 `03:00:09`、手机号。
它们都不是纯数字段，而系统订单号是 19 位雪花 id —— 判据因此收窄成**形状**，不再看文案。
"""

from __future__ import annotations

import re

#: 「这段话在说订单」的显式说法。`order` 的边界只能用 `[A-Za-z]` 写：Python 的 `\b` 把中文
#: 也算作词字符，于是「电费ORDER-1001」里的 `\border\b` **匹配不到**，而它恰恰是我们
#: 要认的那一句。
ORDER_WORD = re.compile(r"订单|訂單|(?<![A-Za-z])order(?![A-Za-z])", re.IGNORECASE)

#: 没有「订单」二字时，一段**纯数字**要被当成单号所需的最少位数。系统单号是 19 位；
#: 站点名里的日期（8 位）、时刻（6 位）、手机号（11 位）都在它之下。
MIN_BARE_DIGITS = 15

#: 整条消息就是一个 token（用户直接粘贴单号）。不含冒号，`03:00:09` 不会被当成整条单号。
WHOLE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: 句子里的纯数字段。日期、时刻、时间戳都被 `-`/`:`/`.`/`T` 切开，凑不够这么长。
DIGIT_RUN = re.compile(r"\d+")


def digit_runs(text: str) -> list[str]:
    """句中 15 位以上的纯数字段 —— 「看起来像系统单号」的那一组。

    **门槛与提取必须看同一组候选。** 曾经门槛只说「句中有长数字段」，提取器却从左到右
    取第一个含数字的 token，于是「分析站点 TEST2026-10-29 这笔单2116…」被放行后，
    真正被查的是站点名里的日期（2026-11-02 评审实测）。这里把候选定成一个函数，
    两处都从这里取。
    """
    if not isinstance(text, str):
        return []
    return [run for run in DIGIT_RUN.findall(text) if len(run) >= MIN_BARE_DIGITS]


def names_order(text: str) -> bool:
    """这条消息是否在指某一张订单（而不是在说站点、时刻或别的编号）。

    三种成立：说了「订单」；整条消息是一个带数字的标识符；句中有 15 位以上的纯数字段。
    带连字符/下划线的单号仍要写出「订单」—— 系统没有短到能被形状认出的那种单号。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if ORDER_WORD.search(text):
        return True
    stripped = text.strip()
    if WHOLE_TOKEN.fullmatch(stripped) and any(char.isdigit() for char in stripped):
        return True
    return bool(digit_runs(text))
