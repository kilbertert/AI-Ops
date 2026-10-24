"""Dify 数据集同步的并发闸（#1056 / ADR-0122 / ADR-0125）。

## 它守的是什么

`POST /v1/internal/knowledge/sync` 在运营打开「添加知识库」弹窗时、以及 Dify 知识库页
每 `visibilitychange` / 60 秒各触发一次。**两个运营（或同一个人的两个标签页）同时打开，
就是两个并发 pass。**

两个 pass 各自读一次 Dify 的 dataset 列表、各自发现"这个库还没有"、各自去建 ——
而 Dify 对 `(tenant, name)` **没有唯一约束**。输的那一方留下的是一条重复的 dataset，
运营会看到两个同名条目，删一个还得先确认哪个是活的。

## 为什么是「排队」而不是「跳过」，也不是「时间下限」

这个模块的前两版都错在**丢**上，而丢掉的那一次恰好是用户最需要它的一次：

1. **第一版：拿不到锁就跳过。** 并发时输的一方直接返回 `created: 0`，于是它所在
   的标签页不刷新列表 —— 运营刚建完的库就在眼前，页面却不动。
2. **第二版：加成功/失败时间下限（15s / 5s）。** 从 RAGFlow 建完库切回 Dify，落进
   「上一次成功才 9 秒前」的窗口里，pass 被静默挡掉。2026-10-24 真机实测：
   `08:16:52 created=0`、`08:17:01 created=0`（相隔 9 秒，被下限吞了）、
   `08:18:14 created=1` 才补上 —— 运营看到的就是"要等一分钟 / 要硬刷新"。

**时间下限是在断言"上游这 15 秒内不会变"，而这恰恰是不成立的**：运营在 RAGFlow 里
建库、然后切回来，就是在这 15 秒里发生的。

所以现在的语义是：**在飞时排队等它跑完，再跑自己这一次。** 排队后的那一次读到的是
最新状态，`created` 回来的是真数字，前端照常刷新。扫码式连打（连开两次弹窗）的代价
是第二次多一次只读比对 —— 幂等，且 `created=0` 时前端不失效列表。

## 它不做什么

- 不重试。一次失败就是一次失败；下一次触发自然会再来。
- 不记忆任何跨请求状态。它只有一把锁，不存时间戳 —— 重启后没有需要重置的东西。
- 不决定同步什么。它只决定"现在要不要跑、要不要等"，`dify_knowledge_datasets` 决定跑什么。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

#: 排队上限。一次 pass 是「读一次 Dify 列表 + 建缺的那几条」，实测秒级；30 秒足够
#: 收掉任何真实并发，又远短于运营会去硬刷新的耐心。超时说明上游卡住了 —— 那时跳过
#: 比让请求无限挂着好，下一次触发（60 秒轮询）会再来。
WAIT_SECONDS = 30.0


class KnowledgeDatasetSyncGuard:
    """One pass at a time; a concurrent caller waits for it, then runs its own."""

    def __init__(self, *, wait: float = WAIT_SECONDS) -> None:
        self._wait = wait
        # ponytail: 一把全局锁，跨租户串行。触发点是「打开弹窗 / 切回页面」这类低频
        # 人工动作，串行几十毫秒不是问题；真出现多租户打满再改成按租户分锁。
        self._lock = threading.Lock()

    def run(self, *, work: Callable[[], Any]) -> str:
        """Run ``work()`` after any in-flight pass. Returns a short outcome; never raises.

        **绝不抛**：调用方在弹窗与页面后台路径上，一次失败不该让用户看到报错
        （ADR-0122 约束 1）。返回值只用来写日志。
        """
        if not self._lock.acquire(timeout=self._wait):
            return "skipped (another sync still in flight)"
        try:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - constraint 1
                return f"failed: {type(exc).__name__}"
        finally:
            self._lock.release()
        applied = len(getattr(result, "writes", ()) or ())
        return f"ok ({applied} write(s))"


__all__ = ["KnowledgeDatasetSyncGuard", "WAIT_SECONDS"]
