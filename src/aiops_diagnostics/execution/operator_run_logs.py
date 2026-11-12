"""现有运行账本的只读元数据投影，不恢复业务结果或推进过期状态。"""

from datetime import UTC, datetime

from aiops_diagnostics.common.scalar_rules import format_stored_time

from .agent_runs import _run_from_row


def read_runs(store, caller, app_id, *, page=1, limit=20, run_id=None):
    # 指纹包含本人、租户、角色与场站范围；权限改变后不沿用签发时的可见性。
    where = """publication_app_id = ? AND scope_fingerprint = ?
        AND operation = 'operator_agent'
        AND json_extract(scope_json, '$.caller.b_user_id') = ?
        AND json_extract(scope_json, '$.effective_tenant_id') = ?"""
    params = [app_id, caller.scope_fingerprint, caller.caller.b_user_id, caller.effective_tenant_id]
    if run_id is not None:
        where += " AND run_id = ?"
        params.append(run_id)
    with store._connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM agent_runs WHERE {where} ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
            (*params, limit + 1, (page - 1) * limit),
        ).fetchall()
    return [metadata(_run_from_row(row)) for row in rows[:limit]], len(rows) > limit


def metadata(run):
    # 只投影受限元数据；完整receipt/snapshot包含业务摘要、权限及内部目标。
    trace = [
        {
            key: event[key]
            for key in ("phase", "elapsed_ms", "duration_ms", "at", "tool", "status")
            if key in event
        }
        for event in getattr(run, "trace", ())[-128:]
    ]
    elapsed = trace[-1]["elapsed_ms"] if trace else None
    if elapsed is None and run.completed_at is not None:
        elapsed = max(0, int((run.completed_at - run.created_at).total_seconds() * 1000))
    expired = run.expires_at <= datetime.now(UTC)
    snapshot = run.snapshot or {}
    mode = snapshot.get("delivery_mode")
    review_reason = (snapshot.get("answer_review") or {}).get("reason")
    if review_reason not in {
        "allowed",
        "unsupported_currency",
        "unsupported_station",
        "unsupported_claim",
        "review_uncertain",
        "review_unavailable",
        "review_quota_exhausted",
        "review_auth_rejected",
        "model_failed",
        "model_quota_exhausted",
        "model_stream_incomplete",
    }:
        review_reason = (
            "review_not_accepted"
            if run.state == "failed"
            and any(event.get("phase") == "review" and event.get("status") == "refused" for event in trace)
            else None
        )
    return {
        "run_id": run.run_id,
        "qa_id": run.qa_id,
        "state": "expired" if expired and run.state in {"issued", "running"} else run.state,
        "created_at": format_stored_time(run.created_at),
        "elapsed_ms": elapsed,
        "elapsed_source": "trace" if trace else "recorded_timestamps",
        "result_expired": expired,
        "delivery_mode": mode
        if run.state == "succeeded" and mode in {"diagnosis", "evidence_only"}
        else None,
        "review_reason": review_reason,
        "trace": trace,
        "source": "java",
        "gateway_version": run.gateway_version,
        "node_build": run.node_build,
        "agent_version": run.expected_agent_version if run.exec_config_confirmed else None,
    }
