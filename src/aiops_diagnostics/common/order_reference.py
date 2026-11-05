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

#: 「订单」后面直接跟单号的那一段。中英分开写：中文可以紧贴（`订单2116…` 是原生写法），
#: 英文必须有分隔（`order 123` / `order no: 123`）。写在同一组里时，`ORDER-1001` 里的
#: `ORDER` 会被当成英文单词再截出一个 `-1001`，同一张单变成两个候选、整条请求被拒。
CHINESE_ORDER = re.compile(r"订单(?:号)?\s*[:：#]?\s*([A-Za-z0-9_.:-]+)")
ENGLISH_ORDER = re.compile(
    r"(?<![A-Za-z])order(?![A-Za-z])(?:\s+(?:id|no|number))?\s*(?:[:：]\s*|\s+)"
    r"([A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)


#: 形如标识符的 token。`电费ORDER-1001` 这种**紧贴中文又没分隔符**的写法只有它能认出来，
#: 所以它只作最后一道兜底，且必须句子里确实有「订单」标记时才用。
TOKEN = re.compile(r"(?<![A-Za-z0-9_.:-])[A-Za-z0-9_-]{6,64}(?![A-Za-z0-9_.:-])")


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


def explicit_orders(text: str) -> list[str]:
    """「订单」二字后面点名的那些单号，**按出现顺序**，保留完整标识符。

    保留前缀是必须的：`订单 A2116050516255272961` 里的 `A` 是单号的一部分，
    只取数字段会去查**另一张单**，查不到时系统说「订单不存在」，看起来像用户写错了。
    抓住的尾字符要去掉句读：`订单 2116…2961.` 里的句号不属于单号。
    """
    if not isinstance(text, str):
        return []
    found = CHINESE_ORDER.findall(text) + ENGLISH_ORDER.findall(text)
    return [item.rstrip(TRAILING_PUNCTUATION) or item for item in found]


def order_candidates(text: str) -> list[str]:
    """这条消息点名的订单，**去重保序**。多于一个就是「说得不清楚」。

    **显式写法优先于形状判据。** 一句里既有 `订单 ORDER-1001` 又有裸写的长单号时，
    按形状挑会静默丢掉短的那个 —— 用户以为问了 A 和 B，系统只答了 B（2026-11-02 交接）。
    只有当全文没有任何「订单」标记时，才退回形状判据（19 位雪花 id、整条消息就是单号）。

    说了「订单」但正则一个都没截到时，用 token 扫描兜底：`电费ORDER-1001` 里 `ORDER`
    紧贴中文、后面也没有分隔符，两组显式正则都匹配不到。

    有显式写法时，**15 位以上的裸数字段也并入候选**：`订单 2115…1 2115…2` 的第二张单
    没有「订单」前缀，却是同一种单号的形状。日期（8 位）、时刻、手机号都凑不够位数。

    「是否已被覆盖」按**这个标识符自己包含的全部数字段**判，不是只看第一个：

    - `订单 A2116…` 的裸数字段是同一个标识符的后半截 → 已被覆盖，不是第二张单；
    - `订单 A123-2116…` 同理（只比首段 `123` 会让尾段被算成第二张）；
    - 但 `订单 A2116… 和 2116…` 里那段数字是**独立出现**的 → 是第二张单，必须澄清。

    最后一条是本轮第一版写错的地方：按数字文本**全局**去重，会把两张共尾号的单并成一张，
    正式入口于是不澄清、只查带前缀那张（2026-11-02 评审实测）。
    """
    named = explicit_orders(text)
    if not named and ORDER_WORD.search(text or ""):
        named = [token for token in TOKEN.findall(text) if any(c.isdigit() for c in token)]
    merged = list(named)
    if named or ORDER_WORD.search(text or ""):
        # 每个标识符占用的字符区间：落在区间内部的数字段属于它，落在外面的是独立候选。
        covered = [(span, item) for item in named for span in _spans_of(item, text)]
        merged += [run for run in digit_runs(text) if not _inside_any(run, text, covered)]
    candidates = merged or _pasted(text) or digit_runs(text)
    seen: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen


#: 显式写法里**不该属于单号**的尾字符：句号、全角句号、逗号、顿号、分号、问号、叹号。
#: `订单 2116…2961.` 的句点是句子的一部分；带上它会让单号校验失败，整次运行白跑。
TRAILING_PUNCTUATION = ".,;:!?。，、；：！？）)」』”\"'"


def _pasted(text: str) -> list[str]:
    """整条消息就是一个标识符（用户直接粘贴单号）时，**它就是单号本身**。

    必须排在形状判据**前面**：`A2116050516255272961` 里有一段 19 位数字，先走
    `digit_runs` 会把前缀丢掉，去查另一张单（2026-11-02 评审实测；原生预览在改动前
    是保留前缀的，这是修出来的回归）。
    """
    stripped = text.strip()
    return [stripped] if WHOLE_TOKEN.fullmatch(stripped) and any(c.isdigit() for c in stripped) else []


def _spans_of(item: str, text: str) -> list[tuple[int, int]]:
    """`item` 在原文里出现的位置。找不到（正则改过形状）就当它没有占用区间。"""
    spans = []
    start = text.find(item)
    while start != -1:
        spans.append((start, start + len(item)))
        start = text.find(item, start + 1)
    return spans


def _inside_any(run: str, text: str, spans: list[tuple[tuple[int, int], str]]) -> bool:
    """这段数字是否**只**出现在某个已认出的标识符内部。

    同一段数字若在别处也独立出现过（`A2116… 和 2116…`），`covered` 里只覆盖第一次出现，
    第二次的位置不在任何区间内，于是它仍会进候选 —— 这正是要的。
    """
    positions = [m.start() for m in DIGIT_RUN.finditer(text) if m.group(0) == run]
    if not positions:
        return False
    return any(
        any(start <= position and position + len(run) <= end for (start, end), _ in spans)
        for position in positions
    ) and all(
        any(start <= position and position + len(run) <= end for (start, end), _ in spans)
        for position in positions
    )


def names_order(text: str) -> bool:
    """这条消息是否在指某一张订单（而不是在说站点、时刻或别的编号）。

    三种成立：说了「订单」；整条消息是一个带数字的标识符；句中有 15 位以上的纯数字段。
    带连字符/下划线的单号仍要写出「订单」—— 系统没有短到能被形状认出的那种单号。
    说了「订单」却没有单号（「我上一个订单是什么情况」）也算成立：由下游去澄清。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(ORDER_WORD.search(text)) or bool(order_candidates(text))
