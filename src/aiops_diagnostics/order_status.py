"""订单状态码 → **业务角色**：本仓唯一的落点（PRD #732）。

`rules.ORDER_STATUS` 回答的是「它**叫什么**」（code → 中文标签，给人读）。
这个模块回答的是「它**是什么角色**」（code → 判定用的语义：充电中 / 正常结束 /
异常结束 / 两种不可控异常 / 未知）。两件事相邻但不同，因此是两张表；
**同一份角色只有这一处**。

## 为什么它必须是一处

在 #732 之前，「5 是异常结束」这句话在本仓存在**五份**拼写：`rules.ORDER_STATUS`
的标签、`engine._analyze_status` 的一套分类词、`ops_analysis.ABNORMAL_STATUS` 的数字常量、
`sources` 里插进 SQL 的 f-string（还另抄了一遍 `status = 1` 表示正常结束）、
以及契约里手写的 `"order_status=5"`。而 `tests/` 里 `status == 5` 与 `ORDER_STATUS`
**各 0 命中** —— 也就是说**删掉权威表、或把 `ABNORMAL_STATUS` 改成 7，测试一次都不会红**。

后果不是抽象的一致性，是**看板与下钻会给出相反答案**：同一张订单，`failure_rate`
的分子说它异常结束，点进诊断却说它正常结束。

## 等价优先

本模块是**纯提取**，不改变任何判定。因此 `role_of` 必须与它替换掉的每一条分支
给出同一答案，包括 `engine._to_int` 的 `int(float(x))`（`"5.0"` 类输入）与
`status_label` 对非数字返回「未知」。逐条等价见 `tests/test_order_status.py`。

## 边界

只含冻结值与纯函数：没有类层次、没有运行时状态、没有第三方依赖（与 `order_visibility.py`、
`async_job_lifecycle.py` 同形）。它**不做**标签（那是 `rules.status_label`）、
**不做**停止原因分类（那是 `rules.classify_stop_reason`）。
"""

from __future__ import annotations

from enum import StrEnum


class StatusRole(StrEnum):
    """一个订单状态码在业务判定里的角色。

    取值域**恰好**是 `rules.ORDER_STATUS` 的 code 集合 —— 两张表对同一 code 的
    覆盖必须一致，`tests/test_order_status.py` 钉住了这一点：往一张表加了 code、
    忘了另一张，测试转红。
    """

    CHARGING = "charging"
    FINISHED = "finished"
    UNCONTROLLABLE_EXCEPTION = "uncontrollable_exception"
    EXCEPTION_ALREADY_HANDLED = "exception_already_handled"
    CONTROLLED_ABNORMAL_END = "controlled_abnormal_end"
    #: 不在公司定义里的取值（含 `None` 与非数字）。**不是**任何一种异常或结束：
    #: 把未知读成正常会低估失败率，读成异常会凭空造出数字，两者都是假陈述。
    UNKNOWN = "unknown"


#: code → 角色。**只有这一张**。键集合与 `rules.ORDER_STATUS` 相等（由测试钉住）。
_ROLE_BY_STATUS: dict[int, StatusRole] = {
    0: StatusRole.CHARGING,
    1: StatusRole.FINISHED,
    2: StatusRole.UNCONTROLLABLE_EXCEPTION,
    3: StatusRole.EXCEPTION_ALREADY_HANDLED,
    5: StatusRole.CONTROLLED_ABNORMAL_END,
}

#: 角色的取值域，供一致性测试与调用方遍历。
STATUS_ROLES = frozenset(_ROLE_BY_STATUS.values()) | {StatusRole.UNKNOWN}

#: 已知状态码的集合。与 `rules.ORDER_STATUS` 的键集合相等。
KNOWN_STATUS_CODES = frozenset(_ROLE_BY_STATUS)


def _to_int(value: object) -> int | None:
    """公司那一列是数值型，但调用方手上常常是字符串。

    `int(float(value))` 而不是 `int(value)`：`engine._to_int` 一直这么写，
    而本模块是**纯提取**，必须与它逐条等价（`"5.0"` 要读成 5，不是未知）。
    `None` 与 `""` 落到 `ValueError`/`TypeError`，都返回 `None`。
    """
    if isinstance(value, bool):
        # `int(True) == 1` —— 把布尔读成一个状态码是没有任何依据的，归未知。
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def role_of(status: object) -> StatusRole:
    """这个状态码在业务上是**什么角色**。

    ``None``、非数字、以及公司定义之外的 code 一律 ``UNKNOWN`` —— 它们是同一个
    「这一列不是我们认识的值」，不分辨「无值」与「奇怪的值」，因为两者的处置相同。
    """
    numeric = _to_int(status)
    if numeric is None:
        return StatusRole.UNKNOWN
    return _ROLE_BY_STATUS.get(numeric, StatusRole.UNKNOWN)


def is_abnormal_end(status: object) -> bool:
    """这一单是否算「异常结束」—— `failure_rate` 的分子判据。

    **口径见 `GLOSSARY.md` 的「异常结束订单」：只计状态 5。** 状态 2（不可控异常）
    与 3（已人工处理）是另外两种状态，不是这个判据的一部分；把 `role_of(...) is not
    CHARGING/FINISHED` 当成「异常结束」会把这三种并成一类，而那个数字就不可复核了。
    """
    return role_of(status) is StatusRole.CONTROLLED_ABNORMAL_END


def is_normal_end(status: object) -> bool:
    """这一单是否算「正常结束」—— 今天只有状态 1。"""
    return role_of(status) is StatusRole.FINISHED


def is_charging(status: object) -> bool:
    """这一单是否仍在充电中（未结束）—— 今天只有状态 0。"""
    return role_of(status) is StatusRole.CHARGING


def normal_end_codes() -> tuple[int, ...]:
    """算「正常结束」的全部 code，由角色派生。今天恰好是单元素 `(1,)`。"""
    return tuple(sorted(code for code, role in _ROLE_BY_STATUS.items() if role is StatusRole.FINISHED))


def abnormal_definition() -> str:
    """对外契约里 `definition.abnormal` 的文本，**由权威格式化成**。

    手写过一次 `"order_status=5"`：那个字符串与它描述的数字会各自漂移，
    而读的人正是靠它知道 `failure_rate` 的分子算了什么。
    """
    codes = abnormal_end_codes()
    if len(codes) == 1:
        return f"order_status={codes[0]}"
    return "order_status in (" + ", ".join(str(code) for code in codes) + ")"


def abnormal_end_codes() -> tuple[int, ...]:
    """算「异常结束」的全部 code，**由角色派生**而不是另写一个常量。

    今天恰好是单元素 `(5,)`。扩成多值是产品口径的事（`GLOSSARY.md` 已定只计 5），
    但真要做时只需改 `_ROLE_BY_STATUS` 一处 —— 这正是把常量换成派生的理由。
    """
    return tuple(
        sorted(code for code, role in _ROLE_BY_STATUS.items() if role is StatusRole.CONTROLLED_ABNORMAL_END)
    )
