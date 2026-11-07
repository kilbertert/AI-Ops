"""「这条运行的结果此刻还能不能交付」的唯一判据。

同一件事此前有四种问法（#1443）：180 秒 TTL、`expired` 终态、`still_valid()` 谓词、
以及直接比较 ``state == "succeeded"``。两条运行表对同一段时间给出相反答案：
`agent_runs.get()` 把 `succeeded` 一并收敛成 `expired`，`chat_dify_runs.get()`
只收敛 `running`，于是一条过了 180 秒的成功记录仍是 `succeeded`，必须靠第二个
谓词补判 —— 而 v6 生产路线（`callback_version == 6`）的两个交付面
（`require_result()` 与 `conversation_context()`）根本没有补判，过期后仍交付正文与历史。

判据只有一条：**记录处于成功态，且 `expires_at > now`**。
窗口比较在本模块只出现一次，调用方不再自己判。

`expired()` 是同一窗口的原语，供「运行/授权仍活跃」这类**非交付**判据复用
（`consumer_tool_grants`、`operator_cpms` 的内联比较）：它们问的不是「能不能交付」，
而是「这一轮还在不在」，但窗口是同一个 180 秒。

本模块只含纯函数与冻结值（沿用 `publication/agent_serviceability.py` 的形态），
不含类层次、运行时状态或第三方依赖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aiops_diagnostics.common.scalar_rules import now_utc

#: 成功态。与 `agent_runs.SUCCEEDED`、`dify_run_store.SUCCEEDED` 同值：
#: 两条运行表各自定义状态名，本模块只做判据，不改状态机。
SUCCEEDED = "succeeded"


class _Windowed(Protocol):
    """任何带 180 秒窗口的记录：运行行、取数授权、工具授权。"""

    expires_at: datetime


class _Result(_Windowed, Protocol):
    """可交付的运行记录：在窗口之上还有成功态。"""

    state: str


def expired(record: _Windowed, *, now: datetime | None = None) -> bool:
    """这条记录的 180 秒窗口是否已经关闭（`expires_at <= now`）。"""
    return record.expires_at <= (now or now_utc())


def deliverable(run: _Result, *, now: datetime | None = None) -> bool:
    """这条运行此刻能不能把结果交出去：成功态，且窗口未关。"""
    return run.state == SUCCEEDED and not expired(run, now=now)
