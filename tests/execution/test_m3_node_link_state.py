"""M3-NODE-LINK-01：把「工具领取成功」与「整轮模型完成」分开。

这一组用例证明 Gateway 侧的两条判据：

1. **启用清单的 run 在领取并缓存快照后仍是 `running`** —— 快照存在只说明取到了数；
   转 `succeeded` 必须等节点显式确认整轮完成。
2. **整轮确认本身要重新证明身份** —— 领取时 `matched` 不授权另一份配置来宣告完成。

旧模式（未启用清单）与迁移前记录的语义逐字不变，这一点由既有
`test_m2_close_contract.py` / `test_m3_gateway_exec_config.py` 继续钉住。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from aiops_diagnostics.execution import agent_runs
from aiops_diagnostics.execution.agent_runs import AgentRunStore

NODE_TOKEN = "node-token-alpha-0123456789"


def _store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(tmp_path / "gateway.db")


def _config(tmp_path: Path):
    from execution.test_agent_run_records import _config as write_config

    return agent_runs.AgentRunConfig.load(write_config(tmp_path))


def _issued(store: AgentRunStore, config, identity: str | None):
    from execution.test_agent_run_records import _scope

    return store.issue(
        scope=_scope(),
        order_no="ORDER-A",
        config=config,
        gateway_version="t",
        expected_agent_version=identity,
    )


def _row(store: AgentRunStore) -> sqlite3.Row:
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM agent_runs").fetchone()
    finally:
        connection.close()


IDENTITY = "a" * 64


# --------------------------------------------------------------------------
# 主状态：快照 ≠ 成功
# --------------------------------------------------------------------------


def test_complete_keeps_running_when_an_identity_was_fixed(tmp_path):
    """启用清单：领取 + 快照之后仍是 `running`，且快照与时间戳都已落库。

    这条就是本片的主判据 —— 旧实现会在这一刻写 `succeeded`，于是「工具领取成功」
    被当成了「整轮完成」。
    """
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    assert store.claim(run.run_id) is True

    assert (
        store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
        is True
    )

    row = _row(store)
    assert row["state"] == agent_runs.RUNNING
    assert row["snapshot_json"]
    assert row["completed_at"] is None
    assert row["actual_node_id"] == "node-a1"


def test_complete_still_succeeds_without_an_identity(tmp_path):
    """未启用清单：语义与今天逐字一致（领取 + 快照 ⇒ succeeded）。"""
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), None)
    assert store.claim(run.run_id) is True

    assert (
        store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
        is True
    )

    row = _row(store)
    assert row["state"] == agent_runs.SUCCEEDED
    assert row["completed_at"] is not None


def test_complete_loop_promotes_a_running_record_with_a_snapshot(tmp_path):
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)

    assert verdict == "completed"
    assert settled is not None and settled.state == agent_runs.SUCCEEDED
    assert settled.exec_config_state == agent_runs.EXEC_CONFIG_MATCHED
    assert _row(store)["completed_at"] is not None


def test_complete_loop_is_idempotent_and_keeps_the_first_timestamp(tmp_path):
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
    store.complete_loop(run.run_id, observed_agent_version=IDENTITY)
    first = _row(store)["completed_at"]

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)

    assert verdict == "already_completed"
    assert settled is not None and settled.state == agent_runs.SUCCEEDED
    assert _row(store)["completed_at"] == first


def test_complete_loop_rejects_a_different_identity(tmp_path):
    """领取时的 matched 不授权另一份配置宣告完成。"""
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version="b" * 64)

    assert verdict == "drift"
    assert settled is not None and settled.state == agent_runs.FAILED
    assert settled.reason == agent_runs.EXEC_CONFIG_DRIFT


def test_complete_loop_requires_a_snapshot(tmp_path):
    """只有 running、没有快照 ⇒ 不存在「整轮完成」。"""
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)

    assert verdict == "incomplete"
    assert settled is not None and settled.state == agent_runs.RUNNING


def test_complete_loop_refuses_after_revoke_and_does_not_resurrect(tmp_path):
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
    assert store.revoke(run.run_id) is True

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)

    assert verdict == "not_active"
    assert settled is not None and settled.state == agent_runs.REVOKED


def test_complete_loop_refuses_after_expiry(tmp_path):
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
    with store._connection(write=True) as connection:  # noqa: SLF001 - 直接拨时钟
        connection.execute(
            "UPDATE agent_runs SET expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run.run_id,),
        )

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)

    assert verdict == "expired"
    assert settled is not None and settled.state == agent_runs.EXPIRED


def test_complete_loop_is_not_tracked_without_an_identity(tmp_path):
    """未启用清单：没有「整轮身份」，不写任何列。"""
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), None)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")

    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=None)

    assert verdict == "not_tracked"
    assert settled is not None and settled.state == agent_runs.SUCCEEDED


def test_fail_settles_a_running_record_with_a_snapshot(tmp_path):
    """整轮失败必须能把「已取数、等待确认」那条也钉成终态。

    旧 `fail()` 只看 issued/running，看似够用；这里钉住的是它**确实**覆盖
    running+快照 —— 否则失败通知会静默丢失。
    """
    store = _store(tmp_path)
    run = _issued(store, _config(tmp_path), IDENTITY)
    store.claim(run.run_id)
    store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")

    assert store.fail(run.run_id, "model_failed") is True

    row = _row(store)
    assert row["state"] == agent_runs.FAILED
    assert row["reason"] == "model_failed"
    # 失败之后确认完成必须被拒绝，且不复活。
    verdict, settled = store.complete_loop(run.run_id, observed_agent_version=IDENTITY)
    assert verdict == "not_active"
    assert settled is not None and settled.state == agent_runs.FAILED
