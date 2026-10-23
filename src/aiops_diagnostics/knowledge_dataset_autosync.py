"""Dify 选择器打开时的自动同步（#1056）。

## 为什么要这个东西

「在 RAGFlow 建一个库，它就该出现在 Dify 里」—— 这是运营对"打通"的全部期待。而
今天的实现只做到一半：库在 RAGFlow 里，`/v1/dify/knowledge` 的清单也**立刻**能看到它
（#925 的自动登记），但 Dify 编排页里的「添加知识库」选择器读的是 **Dify 自己的
dataset 表**，不是我们那份清单。

于是必须有人跑一次 `aiops admin sync-knowledge-datasets`。那个人是运维，不是运营；
运营看到的现象是"我建了库，Dify 没有"。

## 决策：挂在清单路由上，一次请求最多尝试一次

**触发点选在这里是有理由的**：选择器打开 = 运营马上要选一个库。这是唯一一个
"现在不同步就会立刻浪费他的时间"的时刻，也是唯一一个不需要常驻作业的时刻。

三条约束，缺一条都会变成事故：

1. **失败绝不改变清单的返回。** 同步是"让它更方便"，不是"它能不能用"。Dify 控制台
   不可达时，清单必须照常返回 —— 否则一次上游抖动会让运营连已有的库都选不到。
2. **同一租户有最短间隔。** 选择器在打开时可能连打几个请求；没有间隔就是每个请求
   都去 Dify 写一遍。间隔内的请求**不尝试**，直接返回当前清单（下一次打开会补上）。
3. **绝不删。** 同步只做 create / rename / fix_permission / fix_retrieval_model。
   `plan()` 的 `orphans` 只进日志，绝不落成删除动作 —— Dify 没有恢复入口。

## 与 ADR-0096 的关系

ADR-0096 说"不自动同步"，理由是**正文**（`sync-from-dify` 会把运营打到一半的话推给
真实用户）。这里是**建库**：RAGFlow 里出现了一个库，把它同步成一条 dataset，是纯增量
的幂等动作，没有"半成品被推上线"的可能。两件事共用一条命令，但风险不同 —— 所以
这一条只在读路径上收敛，不碰任何正文。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: How long one tenant's last attempt suppresses the next one.
#:
#: The picker opens, Dify issues its listing call, and the operator may reopen the
#: dialog within seconds. Without a floor, every reopen is a write pass against
#: Dify. 30s is long enough to collapse one operator's burst and short enough that
#: a library created after a failed attempt appears on the next open.
RETRY_FLOOR_SECONDS = 30.0


@dataclass
class _Attempt:
    at: float
    ok: bool
    detail: str


class KnowledgeDatasetAutoSync:
    """Idempotent convergence toward "every RAGFlow library is a Dify dataset".

    Deliberately NOT a background job. A timer would keep writing to Dify on
    behalf of tenants nobody is looking at, and would need its own supervision,
    restart semantics and failure reporting. Hanging off the listing route means
    the work happens exactly when a human is waiting for it.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        floor_seconds: float = RETRY_FLOOR_SECONDS,
    ) -> None:
        self._clock = clock
        self._floor = floor_seconds
        self._attempts: dict[str, _Attempt] = {}
        # One pass at a time. Two overlapping passes would each read the dataset
        # list and each decide to create the same dataset — Dify has no unique
        # constraint on (tenant, name), so the loser of that race leaves a
        # duplicate behind. A lock is cheaper than making the plan
        # race-tolerant, and the critical section is bounded by one HTTP call.
        self._lock = threading.Lock()

    def maybe_sync(self, *, tenant_id: str, workspace_id: str, run: Callable[[], Any]) -> str:
        """Run ``run()`` unless this tenant was attempted too recently.

        Returns a short human-readable outcome for the log line; never raises.
        The caller is expected to treat every return value as another
        "no news", because the listing it is about to serve does not depend on
        this having succeeded.
        """
        now = self._clock()
        previous = self._attempts.get(tenant_id)
        if previous is not None and previous.ok and now - previous.at < self._floor:
            return "skipped (recent success)"
        if previous is not None and not previous.ok and now - previous.at < self._floor:
            return f"skipped (recent failure: {previous.detail})"
        if not self._lock.acquire(blocking=False):
            return "skipped (another sync in flight)"
        try:
            now = self._clock()
            previous = self._attempts.get(tenant_id)
            if previous is not None and now - previous.at < self._floor:
                return "skipped (raced)"
            try:
                result = run()
            except Exception as exc:  # noqa: BLE001 - see constraint 1
                self._attempts[tenant_id] = _Attempt(at=now, ok=False, detail=type(exc).__name__)
                return f"failed: {type(exc).__name__}: {exc}"
            created = len(getattr(result, "writes", ()) or ())
            self._attempts[tenant_id] = _Attempt(at=now, ok=True, detail=f"{created} writes")
            return f"ok ({created} write(s))"
        finally:
            self._lock.release()

    def forget(self, tenant_id: str) -> None:
        """Drop the memo for one tenant. Exists for tests and for an explicit
        operator-triggered resync that must not wait out the floor."""
        self._attempts.pop(tenant_id, None)


__all__ = ["KnowledgeDatasetAutoSync", "RETRY_FLOOR_SECONDS"]
