from aiops_diagnostics.knowledge_dataset_autosync import KnowledgeDatasetAutoSync


def test_the_engine_swallows_a_failing_run() -> None:
    """约束 1 的引擎层：run 抛了，maybe_sync 必须回一个字符串而不是抛。"""
    s = KnowledgeDatasetAutoSync(clock=lambda: 0.0)
    out = s.maybe_sync(
        tenant_id="t", workspace_id="w", run=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert out.startswith("failed"), out


def test_a_recent_failure_is_not_retried_immediately() -> None:
    """失败也有下限：否则选择器每打开一次就打一次上游。"""
    now = [0.0]
    s = KnowledgeDatasetAutoSync(clock=lambda: now[0])
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")

    s.maybe_sync(tenant_id="t", workspace_id="w", run=boom)
    now[0] = 5.0
    out = s.maybe_sync(tenant_id="t", workspace_id="w", run=boom)
    assert out.startswith("skipped (recent failure"), out
    assert len(calls) == 1


def test_a_recent_success_is_not_repeated() -> None:
    now = [0.0]
    s = KnowledgeDatasetAutoSync(clock=lambda: now[0])
    calls = []

    def ok():
        calls.append(1)
        return type("R", (), {"writes": []})()

    s.maybe_sync(tenant_id="t", workspace_id="w", run=ok)
    now[0] = 10.0
    out = s.maybe_sync(tenant_id="t", workspace_id="w", run=ok)
    assert out.startswith("skipped (recent success"), out
    assert len(calls) == 1


def test_after_the_floor_it_tries_again() -> None:
    now = [0.0]
    s = KnowledgeDatasetAutoSync(clock=lambda: now[0])
    calls = []

    def ok():
        calls.append(1)
        return type("R", (), {"writes": []})()

    s.maybe_sync(tenant_id="t", workspace_id="w", run=ok)
    now[0] = 31.0
    s.maybe_sync(tenant_id="t", workspace_id="w", run=ok)
    assert len(calls) == 2
