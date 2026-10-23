"""并发闸（#1056 / ADR-0122）。

三条判据，对应 review 指出的那个竞态：

1. **一次只跑一个 pass** —— 两个并发请求不能各自决定"要建"；
2. **成功有下限** —— 连续打开弹窗不能每次全量比对一遍；
3. **失败的下限更短** —— 一次抖动之后，运营重开弹窗应该能重试。

外加一条最容易写错的：**绝不抛**。调用方在「添加知识库」弹窗路径上。
"""

from __future__ import annotations

from aiops_diagnostics.knowledge_dataset_sync_guard import KnowledgeDatasetSyncGuard


class _Result:
    def __init__(self, writes: int) -> None:
        self.writes = tuple(range(writes))


def test_a_successful_pass_is_not_repeated_immediately() -> None:
    now = [0.0]
    guard = KnowledgeDatasetSyncGuard(clock=lambda: now[0])
    calls = []

    guard.run(tenant_id="t", work=lambda: (calls.append(1), _Result(0))[1])
    now[0] = 5.0
    outcome = guard.run(tenant_id="t", work=lambda: (calls.append(1), _Result(0))[1])

    assert outcome.startswith("skipped"), outcome
    assert len(calls) == 1


def test_after_the_floor_it_runs_again() -> None:
    now = [0.0]
    guard = KnowledgeDatasetSyncGuard(clock=lambda: now[0])
    calls = []

    guard.run(tenant_id="t", work=lambda: (calls.append(1), _Result(0))[1])
    now[0] = 16.0
    guard.run(tenant_id="t", work=lambda: (calls.append(1), _Result(0))[1])

    assert len(calls) == 2


def test_a_failure_floor_is_shorter_than_the_success_floor() -> None:
    """一次抖动之后，运营应该能重试 —— 而不是被自己刚才的失败挡住半分钟。"""
    now = [0.0]
    guard = KnowledgeDatasetSyncGuard(clock=lambda: now[0])
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("dify is down")

    guard.run(tenant_id="t", work=boom)
    now[0] = 6.0  # 过了失败下限（5s），没过成功下限（15s）
    guard.run(tenant_id="t", work=boom)

    assert len(calls) == 2


def test_a_failing_pass_never_raises() -> None:
    """调用方在弹窗路径上：闸坏了也不能让弹窗报错。"""
    guard = KnowledgeDatasetSyncGuard(clock=lambda: 0.0)

    def boom():
        raise RuntimeError("kaboom")

    outcome = guard.run(tenant_id="t", work=boom)

    assert outcome.startswith("failed"), outcome


def test_concurrent_passes_do_not_both_run() -> None:
    """第二个 pass 拿不到锁就跳过 —— 它做的是同一件事。"""
    import threading

    guard = KnowledgeDatasetSyncGuard(clock=lambda: 0.0)
    started = threading.Event()
    release = threading.Event()
    second_outcome: list[str] = []

    def slow():
        started.set()
        release.wait(2.0)
        return _Result(0)

    first = threading.Thread(target=lambda: guard.run(tenant_id="a", work=slow))
    first.start()
    assert started.wait(2.0)
    try:
        second_outcome.append(guard.run(tenant_id="b", work=lambda: _Result(0)))
    finally:
        release.set()
        first.join(2.0)

    assert second_outcome[0].startswith("skipped (another sync in flight"), second_outcome
