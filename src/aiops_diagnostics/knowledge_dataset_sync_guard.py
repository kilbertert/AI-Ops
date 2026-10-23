"""Dify 数据集同步的并发闸（#1056 / ADR-0122）。

## 它守的是什么

`POST /v1/internal/knowledge/sync` 会在运营打开「添加知识库」弹窗时触发一次收敛。
**两个运营（或同一个人的两个标签页）同时打开，就是两个并发 pass。**

两个 pass 各自读一次 Dify 的 dataset 列表、各自发现"这个库还没有"、各自去建 ——
而 Dify 对 `(tenant, name)` **没有唯一约束**。输的那一方留下的是一条重复的 dataset，
运营会看到两个同名条目，删一个还得先确认哪个是活的。

## 两件东西

1. **一把非阻塞锁**：同一时刻只跑一个 pass。拿不到锁就跳过 —— 另一个 pass 正在做
   同一件事，等它做完比排队重做更合理。
2. **一个租户级下限**：同一个租户在 `RETRY_FLOOR_SECONDS` 内不重复尝试。连续打开
   弹窗（看一眼就走、再打开）是最常见的操作，没有下限就是每次都全量比对一遍。

**成功与失败都有下限**，但失败的下限短得多（见 `FAILURE_FLOOR_SECONDS`）：
一次上游抖动之后，运营重新打开弹窗时应该**能重试**，而不是被自己刚才的失败挡住
半分钟。

## 它不做什么

- 不重试。一次失败就是一次失败；下一次打开弹窗自然会再来。
- 不写日志之外的状态。进程重启后下限重置 —— 那是对的，重启后本来就该重新比对。
- 不决定同步什么。它只决定"现在要不要跑这一次"，`dify_knowledge_datasets` 决定跑什么。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: 成功之后多久内不再重复尝试。选择器可能在几秒内被重开，15 秒足够收掉这种连打，
#: 又短到"刚建的库"在下次打开时一定可见。
RETRY_FLOOR_SECONDS = 15.0

#: 失败之后多久内不再重复尝试。比成功的短：一次抖动之后，运营重开弹窗应该能重试，
#: 而不是被自己刚才的失败挡住。
FAILURE_FLOOR_SECONDS = 5.0


@dataclass
class _Attempt:
    at: float
    ok: bool
    detail: str = ""


class KnowledgeDatasetSyncGuard:
    """One pass at a time, with a short per-tenant floor between attempts."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        success_floor: float = RETRY_FLOOR_SECONDS,
        failure_floor: float = FAILURE_FLOOR_SECONDS,
    ) -> None:
        self._clock = clock
        self._success_floor = success_floor
        self._failure_floor = failure_floor
        self._attempts: dict[str, _Attempt] = {}
        # 非阻塞：拿不到就说明另一 pass 在跑，跳过比排队正确（它做的是同一件事）。
        self._lock = threading.Lock()

    def run(self, *, tenant_id: str, work: Callable[[], Any]) -> str:
        """Run ``work()`` unless suppressed. Returns a short outcome; never raises.

        **绝不抛**：调用方是「添加知识库」弹窗路径上的一次后台调用，一次失败不该
        让弹窗报错（ADR-0122 约束 1）。返回值只用来写日志。
        """
        now = self._clock()
        previous = self._attempts.get(tenant_id)
        if previous is not None:
            floor = self._success_floor if previous.ok else self._failure_floor
            if now - previous.at < floor:
                return "skipped (recent)"

        if not self._lock.acquire(blocking=False):
            # 另一个 pass 正在跑。**不更新本租户的时间戳** —— 那次 pass 可能属于
            # 别的租户，把这次记成"刚跑过"会让本租户在它结束后被误挡。
            return "skipped (another sync in flight)"
        try:
            now = self._clock()
            previous = self._attempts.get(tenant_id)
            if previous is not None:
                floor = self._success_floor if previous.ok else self._failure_floor
                if now - previous.at < floor:
                    return "skipped (raced)"
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - constraint 1
                self._attempts[tenant_id] = _Attempt(at=now, ok=False, detail=type(exc).__name__)
                return f"failed: {type(exc).__name__}"
            applied = len(getattr(result, "writes", ()) or ())
            self._attempts[tenant_id] = _Attempt(at=now, ok=True, detail=f"{applied} writes")
            return f"ok ({applied} write(s))"
        finally:
            self._lock.release()

    def forget(self, tenant_id: str) -> None:
        """Drop the memo for one tenant — for an explicit operator-triggered resync."""
        self._attempts.pop(tenant_id, None)


__all__ = [
    "FAILURE_FLOOR_SECONDS",
    "KnowledgeDatasetSyncGuard",
    "RETRY_FLOOR_SECONDS",
]
