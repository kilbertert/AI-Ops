"""交付判据的唯一权威与两引擎一致性（#1443）。

两条引擎对「这条运行的结果此刻还能不能交付」必须给出**同一个答案**：

- `agent_runs.AgentRun`：`get()` 会把 `succeeded` 收敛成 `expired`；
- `dify_run_store.DifyRun`：`get()` 只把 `running` 收敛成 `expired`，
  一条过了 180 秒的 `succeeded` 行**仍然是** `succeeded`。

行状态因此不能作为交付判据，判据只能落在 `deliverable()` 上。这条用例把同一个
`(state, expires_at)` 喂给两种记录，断言答案一致。
"""

from datetime import UTC, datetime, timedelta

from aiops_diagnostics.dify.dify_run_store import DifyRun, still_valid
from aiops_diagnostics.execution.agent_runs import AgentRun
from aiops_diagnostics.execution.run_deliverability import deliverable, expired

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


def _agent_run(state, expires_at):
    return AgentRun(
        run_id="run-1",
        scope_fingerprint="fp",
        scope_json="{}",
        order_no="",
        operation="consumer_service",
        environment="env-a",
        app_id="app",
        agent_id="agent",
        expected_node_id="node",
        state=state,
        reason=None,
        snapshot=None,
        actual_node_id=None,
        node_build=None,
        expected_agent_version=None,
        observed_agent_version=None,
        exec_config_state=None,
        gateway_version="",
        created_at=NOW,
        expires_at=expires_at,
        claimed_at=None,
        completed_at=None,
    )


def _dify_run(state, expires_at):
    return DifyRun(
        run_id="run-1",
        qa_id="qa-1",
        scope_fingerprint="fp",
        scope_json="{}",
        binding_key="key",
        order_no="",
        environment="env-a",
        tenant_id="TENANT-A",
        app_id="app",
        workflow_id="wf",
        workflow_run_id=None,
        task_id=None,
        grant_id="grant",
        state=state,
        reason=None,
        created_at=NOW,
        expires_at=expires_at,
        completed_at=None,
    )


def test_both_engines_answer_the_same_for_the_same_window():
    cases = [
        ("succeeded", NOW + timedelta(seconds=179), True),
        ("succeeded", NOW + timedelta(seconds=1), True),
        ("succeeded", NOW, False),
        ("succeeded", NOW - timedelta(seconds=1), False),
        ("running", NOW + timedelta(seconds=179), False),
        ("failed", NOW + timedelta(seconds=179), False),
        ("expired", NOW + timedelta(seconds=179), False),
    ]
    for state, expires_at, expected in cases:
        agent = _agent_run(state, expires_at)
        dify = _dify_run(state, expires_at)
        assert deliverable(agent, now=NOW) is expected, (state, expires_at)
        assert deliverable(dify, now=NOW) is expected, (state, expires_at)


def test_the_window_is_judged_identically_for_both_records():
    for offset in (timedelta(seconds=179), timedelta(0), -timedelta(seconds=1)):
        expires_at = NOW + offset
        agent = _agent_run("succeeded", expires_at)
        dify = _dify_run("succeeded", expires_at)
        assert expired(agent, now=NOW) is expired(dify, now=NOW) is (offset <= timedelta(0))
        assert still_valid(dify, now=NOW) is not expired(dify, now=NOW)


def test_expiry_is_inclusive_of_the_deadline():
    """`expires_at == now` 视为已过期 —— 与 `agent_runs._expire_in` 的 `<=` 一致。"""
    at_deadline = _dify_run("succeeded", NOW)
    assert expired(at_deadline, now=NOW) is True
    assert deliverable(at_deadline, now=NOW) is False
    assert deliverable(at_deadline, now=NOW - timedelta(microseconds=1)) is True
