"""交付组 B1：请求级授权记录的存储与生命周期（不经过 HTTP）。

路由层的回归在 B2；这一片只证明「记录本身」的判据：
原子领取、过期/撤销/拒绝释放、冻结身份的序列化、绑定表的凭据映射。
"""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops_diagnostics import agent_runs
from aiops_diagnostics.agent_runs import (
    EXPIRED,
    ISSUED,
    REVOKED,
    RUNNING,
    SUCCEEDED,
    AgentRunConfig,
    AgentRunConfigError,
    AgentRunStore,
    scope_from_json,
    scope_to_json,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

NODE_TOKEN = "node-token-alpha-0123456789"


def _scope(c_user: str = "USER-A") -> ScopeContext:
    subject = SubjectRecord(b_user_id="", c_user_id=c_user, tenant_id="TENANT-A")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="TENANT-A",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
        resolved_at=datetime(2026, 10, 17, tzinfo=UTC),
    )


def _config(tmp_path: Path, *, active: bool = True) -> Path:
    path = tmp_path / "agent-runs.json"
    path.write_text(
        json.dumps(
            {
                "environment": "env-a",
                "snail_base_url": "http://127.0.0.1:1",
                "snail_app_id": "aiops-alpha",
                "snail_app_token": "app-token-alpha",
                "snail_agent_id": 7,
                "snail_open_id": "open-id-alpha",
                "expected_node_id": "node-a1",
                "nodes": [
                    {
                        "node_id": "node-a1",
                        "environment": "env-a",
                        "active": active,
                        "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                    }
                ],
            }
        )
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


@pytest.fixture
def store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(tmp_path / "gateway.db")


def test_issue_freezes_identity_and_binding(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="0.1.0")
    assert run.state == ISSUED
    assert run.environment == "env-a" and run.app_id == "aiops-alpha" and run.agent_id == "7"
    assert run.expected_node_id == "node-a1"
    assert scope_from_json(run.scope_json).scope_fingerprint == run.scope_fingerprint
    assert 30 <= agent_runs.RUN_TTL_SECONDS <= 600


def test_claim_is_atomic_across_threads(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    results: list[bool] = []
    barrier = threading.Barrier(4)

    def claim() -> None:
        barrier.wait()
        results.append(store.claim(run.run_id))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]
    assert store.get(run.run_id).state == RUNNING


def test_expiry_converges_and_blocks_claim(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE agent_runs SET expires_at = ? WHERE run_id = ?",
            (datetime(2026, 10, 17, tzinfo=UTC).isoformat(), run.run_id),
        )
    assert store.get(run.run_id).state == EXPIRED
    assert store.claim(run.run_id) is False


def test_revoke_and_refuse_are_terminal(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    first = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.revoke(first.run_id) is True
    assert store.get(first.run_id).state == REVOKED

    second = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(second.run_id) is True
    assert store.complete(second.run_id, snapshot={"x": 1}, actual_node_id="node-a1", node_build="b") is True
    assert store.get(second.run_id).state == SUCCEEDED
    # refuse 只对 succeeded 生效：结果已算出但放行复核失败。
    assert store.refuse(second.run_id, "node_deactivated") is True
    stored = store.get(second.run_id)
    assert stored.state == agent_runs.FAILED and stored.reason == "node_deactivated"
    assert store.refuse(second.run_id, "again") is False


def test_restart_does_not_requeue_running(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(run.run_id) is True
    reopened = AgentRunStore(tmp_path / "gateway.db")
    assert reopened.get(run.run_id).state == RUNNING
    assert reopened.claim(run.run_id) is False


def test_scope_snapshot_refuses_tampering():
    scope = _scope()
    assert scope_from_json(scope_to_json(scope)).scope_fingerprint == scope.scope_fingerprint
    tampered = json.loads(scope_to_json(scope))
    tampered["effective_tenant_id"] = "TENANT-B"
    with pytest.raises(ValueError):
        scope_from_json(json.dumps(tampered))


def test_config_file_must_be_private_and_active(tmp_path):
    path = _config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)
    path.chmod(0o600)
    config = AgentRunConfig.load(path)
    assert config.authenticate_node(NODE_TOKEN) is not None
    assert config.authenticate_node("wrong") is None
    assert config.authenticate_node("") is None


def test_inactive_node_cannot_authenticate(tmp_path):
    directory = tmp_path / "sub"
    directory.mkdir()
    config = AgentRunConfig.load(_config(directory, active=False))
    assert config.authenticate_node(NODE_TOKEN) is None


def test_complete_after_revoke_stores_nothing(store, tmp_path):
    """撤销后即使节点把结果交回来，也不能落库 —— 这条路径是「撤销后仍返回快照」的存储侧根因。"""
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(run.run_id) is True
    assert store.revoke(run.run_id) is True
    assert (
        store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
        is False
    )
    stored = store.get(run.run_id)
    assert stored.state == REVOKED and stored.snapshot is None


def test_open_preserves_existing_tables_and_records(tmp_path):
    """建表只允许 `IF NOT EXISTS`：同一个 Gateway 库里还有别的表与既有记录。"""
    database = tmp_path / "gateway.db"
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE unrelated (k TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO unrelated VALUES ('keep-me')")
        connection.commit()
    finally:
        connection.close()
    first = AgentRunStore(database)
    config = AgentRunConfig.load(_config(tmp_path))
    run = first.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    # 再打开一次（升级/重启路径）：既有表与记录都还在，旧记录不被改写。
    reopened = AgentRunStore(database)
    assert reopened.get(run.run_id).state == ISSUED
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT k FROM unrelated").fetchall() == [("keep-me",)]
    finally:
        connection.close()


def test_config_rejects_missing_digest_and_unregistered_expected_node(tmp_path):
    """绑定表的两种坏形态都必须**拒绝构造**，而不是带着不完整的绑定继续跑。"""
    good = json.loads(_config(tmp_path).read_text())

    bad_digest = {**good, "nodes": [{**good["nodes"][0], "token_sha256": "not-a-digest"}]}
    path = tmp_path / "bad-digest.json"
    path.write_text(json.dumps(bad_digest))
    path.chmod(0o600)
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)

    unregistered = {**good, "expected_node_id": "node-elsewhere"}
    path = tmp_path / "unregistered.json"
    path.write_text(json.dumps(unregistered))
    path.chmod(0o600)
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)

    path = tmp_path / "not-json.json"
    path.write_text("{not json")
    path.chmod(0o600)
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)


def test_fail_is_terminal_from_issued_and_running(store, tmp_path):
    config = AgentRunConfig.load(_config(tmp_path))
    issued = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.fail(issued.run_id, "node_error") is True
    assert store.get(issued.run_id).state == agent_runs.FAILED
    assert store.fail(issued.run_id, "again") is False

    running = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(running.run_id) is True
    assert store.fail(running.run_id, "node_error", actual_node_id="node-a1") is True
    assert store.get(running.run_id).actual_node_id == "node-a1"


def test_receipt_carries_snapshot_digest_and_no_agent_version(store, tmp_path):
    """回执的字段只来自服务端记录：有快照时附摘要，版本缺口显式标注。"""
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(run.run_id) is True
    assert (
        store.complete(run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b")
        is True
    )
    receipt = store.get(run.run_id).receipt()
    assert receipt["agent_version"] is None
    assert receipt["agent_version_gap"] == agent_runs.AGENT_VERSION_GAP
    assert (
        receipt["result_sha256"]
        == hashlib.sha256(json.dumps({"order_no": "ORDER-A"}, sort_keys=True).encode()).hexdigest()
    )
    assert receipt["actual_node_id"] == "node-a1"


def test_missing_config_and_write_failure_do_not_escape_their_contracts(tmp_path):
    """两条失败路径：配置不存在要变成明确的配置错误；写事务异常要回滚后原样抛出。"""
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(tmp_path / "does-not-exist.json")

    store = AgentRunStore(tmp_path / "gateway.db")
    with pytest.raises(RuntimeError), store._connection(write=True) as connection:
        connection.execute("CREATE TABLE rolled_back (k TEXT)")
        raise RuntimeError("boom")
    import sqlite3

    connection = sqlite3.connect(tmp_path / "gateway.db")
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert "rolled_back" not in tables


@pytest.mark.parametrize("active", ["false", "true", 0, 1, None, {}])
def test_config_rejects_non_boolean_active(tmp_path, active):
    path = _config(tmp_path)
    raw = json.loads(path.read_text())
    raw["nodes"][0]["active"] = active
    path.write_text(json.dumps(raw))
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)


@pytest.mark.parametrize("digest", ["g" * 64, "é" * 64, 0, None, "a" * 63, "a" * 65])
def test_config_rejects_malformed_digest(tmp_path, digest):
    path = _config(tmp_path)
    raw = json.loads(path.read_text())
    raw["nodes"][0]["token_sha256"] = digest
    path.write_text(json.dumps(raw))
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)


@pytest.mark.parametrize("uppercase", [False, True])
def test_config_rejects_duplicate_credential_identity(tmp_path, uppercase):
    path = _config(tmp_path, active=False)
    raw = json.loads(path.read_text())
    duplicate = {**raw["nodes"][0], "active": True}
    if uppercase:
        duplicate["token_sha256"] = duplicate["token_sha256"].upper()
    raw["nodes"].append(duplicate)
    path.write_text(json.dumps(raw))
    with pytest.raises(AgentRunConfigError):
        AgentRunConfig.load(path)


@pytest.mark.parametrize("fingerprint", ["", None, 0, "g" * 64, "a" * 63])
def test_scope_snapshot_never_recreates_invalid_stored_fingerprint(fingerprint):
    raw = json.loads(scope_to_json(_scope()))
    raw["effective_tenant_id"] = "TENANT-B"
    raw["scope_fingerprint"] = fingerprint
    with pytest.raises(ValueError):
        scope_from_json(json.dumps(raw))


def test_complete_at_expiry_without_prior_read_stores_nothing(store, tmp_path, monkeypatch):
    config = AgentRunConfig.load(_config(tmp_path))
    run = store.issue(scope=_scope(), order_no="ORDER-A", config=config, gateway_version="t")
    assert store.claim(run.run_id) is True
    monkeypatch.setattr(agent_runs, "_now", lambda: run.expires_at)
    # 期限到达后不先 get()：complete 自己必须拒绝，不能依赖读取时的过期收敛。
    assert not store.complete(
        run.run_id, snapshot={"order_no": "ORDER-A"}, actual_node_id="node-a1", node_build="b"
    )
    stored = store.get(run.run_id)
    assert stored.state == EXPIRED and stored.snapshot is None
