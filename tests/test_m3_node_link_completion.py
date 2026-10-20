"""M3-NODE-LINK-01 的 HTTP 判据：整轮终局通知端点。

走**真实路由**（与领取同一个 `create_gateway_app`），只把外部依赖（CPMS、编排平台、
订单授权）替换成既有夹具。这里钉住的是：

- 完成通知**只在已核对身份、仍在 running、已有快照、未过期**时才转 succeeded；
- 错摘要/错节点/未带身份一律拒绝，且**不伤他人记录**；
- 重复通知幂等，冲突拒绝，终态不复活；
- 响应**不含业务快照**；
- 未启用清单时该端点明确「不追踪」，不改变旧行为。
"""

from __future__ import annotations

import pytest

NODE_TOKEN = "node-token-alpha-0123456789"


def _fixture(tmp_path, monkeypatch, *, manifest):
    from test_m3_gateway_exec_config import _fixture as build

    return build(tmp_path, monkeypatch, manifest=manifest)


def _delegate(fixture):
    from test_m3_gateway_exec_config import _delegate as delegate

    return delegate(fixture)


def _claim(fixture, run_id, **headers):
    from test_m3_gateway_exec_config import _claim as claim

    return claim(fixture, run_id, **headers)


def _rows(fixture):
    from test_m3_gateway_exec_config import _rows as rows

    return rows(fixture["store"])


def _run_id(fixture):
    from test_m3_gateway_exec_config import _run_id as run_id

    return run_id(fixture)


def _complete(fixture, run_id, *, status="completed", reason=None, identity=None, token=NODE_TOKEN):
    body = {"status": status}
    if reason is not None:
        body["reason"] = reason
    headers = {"Authorization": f"Bearer {token}"}
    if identity is not None:
        headers["X-AI-Ops-Exec-Config"] = identity
    return fixture["client"].post(
        f"/v1/internal/charging-status/completions/{run_id}", headers=headers, json=body
    )


def _write_manifest(tmp_path):
    from test_m3_gateway_exec_config import _write_manifest as write

    return write(tmp_path)


def _identity(path):
    from test_m3_gateway_exec_config import _identity as identity

    return identity(path)


def _claimed_with_snapshot(tmp_path, monkeypatch, manifest):
    """签一条 run、领取（拿到快照），返回 (fixture, run_id, identity)。"""
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502  # 派发期不领取，由测试扮演节点
    run_id = _run_id(fixture)
    claimed = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity})
    assert claimed.status_code == 200, claimed.text
    return fixture, run_id, identity


# --------------------------------------------------------------------------
# 正例与幂等
# --------------------------------------------------------------------------


def test_claim_caches_the_snapshot_but_does_not_succeed_until_completion(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)

    row = _rows(fixture)[0]
    assert row["state"] == "running"
    assert row["snapshot_json"]
    assert row["completed_at"] is None

    done = _complete(fixture, run_id, identity=identity)

    assert done.status_code == 200, done.text
    assert done.json() == {"run_id": run_id, "state": "succeeded"}
    assert "order_no" not in done.json(), "完成通知不是领取端点，不返回业务快照"
    assert _rows(fixture)[0]["state"] == "succeeded"


def test_completion_is_idempotent_and_keeps_the_first_timestamp(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    assert _complete(fixture, run_id, identity=identity).status_code == 200
    first = _rows(fixture)[0]["completed_at"]

    again = _complete(fixture, run_id, identity=identity)

    assert again.status_code == 200, again.text
    assert again.json()["state"] == "succeeded"
    assert _rows(fixture)[0]["completed_at"] == first


def test_failed_completion_settles_the_record_without_a_snapshot_reply(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)

    failed = _complete(fixture, run_id, status="failed", reason="model_failed", identity=identity)

    assert failed.status_code == 200, failed.text
    assert "order_no" not in failed.json()
    row = _rows(fixture)[0]
    assert row["state"] == "failed"
    assert row["reason"] == "model_failed"
    # 失败之后不能再被确认成功。
    assert _complete(fixture, run_id, identity=identity).status_code in {403, 409}


def test_completion_after_failure_cannot_succeed(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    _complete(fixture, run_id, status="failed", reason="model_failed", identity=identity)

    late = _complete(fixture, run_id, identity=identity)

    assert late.status_code == 403
    assert late.json()["error"]["code"] == "AGENT_RUN_NOT_ACTIVE"


# --------------------------------------------------------------------------
# 拒绝：身份、节点、终态、未启用
# --------------------------------------------------------------------------


def test_completion_with_a_wrong_identity_is_refused(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, _ = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)

    response = _complete(fixture, run_id, identity="0" * 64)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"
    row = _rows(fixture)[0]
    assert row["state"] == "failed"
    assert row["reason"] == "exec_config_drift"


def test_completion_without_the_identity_header_is_refused(tmp_path, monkeypatch):
    """领取时的 matched 不授权另一次不带身份的提交。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, _ = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)

    response = _complete(fixture, run_id)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_RUN_EXEC_CONFIG_DRIFT"
    assert _rows(fixture)[0]["state"] == "failed"


def test_completion_from_a_sibling_node_does_not_touch_the_record(tmp_path, monkeypatch):
    """错节点只读判定、不写任何列，也不影响别人的记录。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    before = dict(_rows(fixture)[0])

    # 同环境、但**不是**记录里 expected_node_id 的那个节点的凭据。
    extra = {
        "node_id": "node-a2",
        "environment": "env-a",
        "active": True,
        "token_sha256": __import__("hashlib").sha256(b"sibling-token").hexdigest(),
    }
    config_path = fixture["app"].state.gateway.settings.agent_run_config_file
    import json as _json
    import stat as _stat
    from pathlib import Path as _Path

    payload = _json.loads(_Path(config_path).read_text())
    payload["nodes"].append(extra)
    _Path(config_path).write_text(_json.dumps(payload))
    _Path(config_path).chmod(_stat.S_IRUSR | _stat.S_IWUSR)

    response = _complete(fixture, run_id, identity=identity, token="sibling-token")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_RUN_WRONG_NODE"
    assert _rows(fixture)[0] == before


def test_completion_after_revoke_is_refused(tmp_path, monkeypatch):
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    store = fixture["app"].state.gateway.agent_run_store
    assert store.revoke(run_id) is True

    response = _complete(fixture, run_id, identity=identity)

    assert response.status_code == 403
    assert _rows(fixture)[0]["state"] == "revoked"


def test_completion_without_a_manifest_is_not_tracked(tmp_path, monkeypatch):
    """未启用清单：端点存在但不追踪，旧行为逐字不变（领取直接 succeeded）。"""
    fixture = _fixture(tmp_path, monkeypatch, manifest=None)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)
    assert _claim(fixture, run_id).status_code == 200
    assert _rows(fixture)[0]["state"] == "succeeded"

    response = _complete(fixture, run_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_RUN_COMPLETION_NOT_TRACKED"
    assert _rows(fixture)[0]["state"] == "succeeded"


def test_completion_before_any_snapshot_is_a_conflict(tmp_path, monkeypatch):
    """issued / running 无快照不存在「整轮完成」。"""
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    fixture = _fixture(tmp_path, monkeypatch, manifest=manifest)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)

    response = _complete(fixture, run_id, identity=identity)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_RUN_COMPLETION_CONFLICT"
    assert _rows(fixture)[0]["state"] == "issued"


def test_completion_body_rejects_extra_fields(tmp_path, monkeypatch):
    """严格体：多一个字段就是 422，而不是被忽略（不能借它夹带业务数据）。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)

    response = fixture["client"].post(
        f"/v1/internal/charging-status/completions/{run_id}",
        headers={"Authorization": f"Bearer {NODE_TOKEN}", "X-AI-Ops-Exec-Config": identity},
        json={"status": "completed", "order_no": "ORDER-A"},
    )

    assert response.status_code == 422
    assert _rows(fixture)[0]["state"] == "running", "被拒的请求不得改状态"


# --------------------------------------------------------------------------
# 领取重试：同一份快照，业务只读一次
# --------------------------------------------------------------------------


def test_retry_claim_after_snapshot_returns_the_same_snapshot_once(tmp_path, monkeypatch):
    """新模式下的「已取数、等待确认」也必须走同一条重试语义：业务只读一次。"""
    manifest = _write_manifest(tmp_path)
    identity = _identity(manifest)
    from test_m2_close_contract import cpms_stub

    ctx = cpms_stub()
    upstream = ctx.__enter__()
    try:
        from test_m2_close_contract import _app as build_app

        fixture = build_app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        fixture["dispatcher"].claim = None
        settings = fixture["app"].state.gateway.settings
        object.__setattr__(settings, "execution_manifest_file", str(manifest))
        assert _delegate(fixture).status_code == 502
        run_id = _run_id(fixture)

        first = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity})
        second = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity})

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["order_no"] == second.json()["order_no"]
        assert upstream["calls"] == 1, "重试不得再读一次业务"
        assert _rows(fixture)[0]["state"] == "running"
    finally:
        ctx.__exit__(None, None, None)


def test_gateway_refuses_to_release_after_revoke_between_claim_and_completion(tmp_path, monkeypatch):
    """撤销发生在「已取数、待确认」之间 ⇒ 完成通知拒绝，且不会再交出快照。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    store = fixture["app"].state.gateway.agent_run_store
    assert store.revoke(run_id) is True

    again = _claim(fixture, run_id, **{"X-AI-Ops-Exec-Config": identity})
    done = _complete(fixture, run_id, identity=identity)

    assert again.status_code == 403
    assert done.status_code == 403
    assert "order_no" not in again.json()


@pytest.mark.parametrize("offset_seconds", [-1, 0, 1])
def test_completion_expiry_boundary_matches_the_claim_semantics(tmp_path, monkeypatch, offset_seconds):
    """到期判据仍是 ``expires_at <= now``：到期前放行、**恰好到期**拒绝、明显过期拒绝。"""
    from datetime import timedelta

    from aiops_diagnostics import agent_runs

    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    expires = fixture["store"].get(run_id).expires_at
    monkeypatch.setattr(agent_runs, "_now", lambda e=expires, o=offset_seconds: e + timedelta(seconds=o))

    response = _complete(fixture, run_id, identity=identity)

    if offset_seconds < 0:
        assert response.status_code == 200, response.text
        assert _rows(fixture)[0]["state"] == "succeeded"
    else:
        assert response.status_code == 403
        assert _rows(fixture)[0]["state"] == "expired"


# --------------------------------------------------------------------------
# 复核反例（独立复现脚本 test_node_link_review.py 的三条，固化进仓库）
# --------------------------------------------------------------------------


def test_untracked_run_failure_notice_does_not_change_legacy_record(tmp_path, monkeypatch):
    """未启用清单的旧记录收到「失败通知」时什么都不该发生。

    反例：当时 `observed is None == expected is None`，于是把一条 M2 记录写成了 failed。
    """
    fixture = _fixture(tmp_path, monkeypatch, manifest=None)
    assert _delegate(fixture).status_code == 502
    run_id = _run_id(fixture)
    before = dict(_rows(fixture)[0])

    response = _complete(fixture, run_id, status="failed", reason="model_failed")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_RUN_COMPLETION_NOT_TRACKED"
    assert _rows(fixture)[0] == before


def test_wrong_identity_completion_keeps_terminal_record_untouched(tmp_path, monkeypatch):
    """已确认完成的记录收到错摘要通知：拒绝，且审计列保持原样。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    assert _complete(fixture, run_id, identity=identity).status_code == 200
    before = dict(_rows(fixture)[0])

    response = _complete(fixture, run_id, identity="0" * 64)

    assert response.status_code == 403
    assert _rows(fixture)[0] == before
    assert before["exec_config_state"] == "matched"


def test_conflicting_failure_reason_is_rejected_not_idempotent(tmp_path, monkeypatch):
    """同状态、不同原因的通知是**冲突**，不是幂等成功。"""
    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    first = _complete(fixture, run_id, status="failed", reason="model_failed", identity=identity)
    assert first.status_code == 200
    before = dict(_rows(fixture)[0])

    second = _complete(fixture, run_id, status="failed", reason="model_stream_incomplete", identity=identity)

    assert second.status_code == 409
    assert _rows(fixture)[0] == before


def test_failure_notice_after_expiry_does_not_overwrite_ttl_semantics(tmp_path, monkeypatch):
    """到期之后再来的失败通知不能把 expired 改写成 model_failed。"""
    from datetime import timedelta

    from aiops_diagnostics import agent_runs

    manifest = _write_manifest(tmp_path)
    fixture, run_id, identity = _claimed_with_snapshot(tmp_path, monkeypatch, manifest)
    expires = fixture["store"].get(run_id).expires_at
    monkeypatch.setattr(agent_runs, "_now", lambda e=expires: e + timedelta(seconds=1))

    response = _complete(fixture, run_id, status="failed", reason="model_failed", identity=identity)

    assert response.status_code == 403
    row = _rows(fixture)[0]
    assert row["state"] == "expired"
    assert row["reason"] == "ttl_elapsed"
