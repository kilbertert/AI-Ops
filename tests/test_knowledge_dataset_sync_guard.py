"""并发闸（#1056 / ADR-0122 / ADR-0125）。

三条判据：

1. **一次只跑一个 pass** —— 两个并发请求不能各自决定"要建"；
2. **后来者排队，不跳过** —— 被跳过的恰好是「刚在 RAGFlow 建完库切回来」那一次，
   真机实测过它被 15 秒下限吞掉（ADR-0125）；
3. **绝不抛** —— 调用方在弹窗与页面后台路径上。
"""

from __future__ import annotations

import threading

from aiops_diagnostics.knowledge_dataset_sync_guard import KnowledgeDatasetSyncGuard


class _Result:
    def __init__(self, writes: int) -> None:
        self.writes = tuple(range(writes))


def test_every_call_runs_the_work() -> None:
    """没有时间下限：连续两次调用都真的跑过。

    这就是真机那个 bug 的判据 —— 旧版第二次返回 `skipped (recent)`，运营刚建的库
    因此不出现（ADR-0125）。
    """
    guard = KnowledgeDatasetSyncGuard()
    calls = []

    first = guard.run(work=lambda: (calls.append(1), _Result(0))[1])
    second = guard.run(work=lambda: (calls.append(1), _Result(1))[1])

    assert first.startswith("ok"), first
    assert second.startswith("ok"), second
    assert len(calls) == 2


def test_a_concurrent_caller_waits_and_then_runs() -> None:
    """在飞时不丢：第二个 pass 等第一个跑完，自己再跑一次。"""
    guard = KnowledgeDatasetSyncGuard()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append("slow")
        started.set()
        release.wait(2.0)
        return _Result(0)

    first = threading.Thread(target=lambda: guard.run(work=slow))
    first.start()
    assert started.wait(2.0)

    second: list[str] = []

    def second_call():
        # 排队上限远大于这里的 2 秒，所以它一定等得到锁。
        second.append(guard.run(work=lambda: (calls.append("fast"), _Result(1))[1]))

    waiter = threading.Thread(target=second_call)
    waiter.start()
    release.set()
    first.join(2.0)
    waiter.join(2.0)

    assert second[0].startswith("ok"), second
    assert calls == ["slow", "fast"], calls


def test_a_wedged_pass_is_skipped_after_the_wait_bound() -> None:
    """上游卡死时，排队有上限 —— 让请求挂着比跳过糟。"""
    guard = KnowledgeDatasetSyncGuard(wait=0.05)
    hold = threading.Event()
    started = threading.Event()

    def wedged():
        started.set()
        hold.wait(2.0)
        return _Result(0)

    stuck = threading.Thread(target=lambda: guard.run(work=wedged))
    stuck.start()
    assert started.wait(2.0)
    try:
        outcome = guard.run(work=lambda: _Result(0))
    finally:
        hold.set()
        stuck.join(2.0)

    assert outcome.startswith("skipped"), outcome


def test_a_failing_pass_never_raises() -> None:
    """调用方在弹窗路径上：闸坏了也不能让弹窗报错。"""
    guard = KnowledgeDatasetSyncGuard()

    def boom():
        raise RuntimeError("kaboom")

    outcome = guard.run(work=boom)

    assert outcome.startswith("failed"), outcome


def test_a_failure_does_not_block_the_next_attempt() -> None:
    """一次抖动之后运营应该能重试 —— 不是被自己刚才的失败挡住。"""
    guard = KnowledgeDatasetSyncGuard()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("dify is down")

    guard.run(work=boom)
    guard.run(work=lambda: (calls.append(1), _Result(1))[1])

    assert len(calls) == 2
