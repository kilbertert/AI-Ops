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
    # 禁止复用 receipt()/snapshot：其中包含业务摘要、范围及内部执行目标。
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
    return {
        "run_id": run.run_id,
        "qa_id": run.qa_id,
        "state": "expired" if expired and run.state in {"issued", "running"} else run.state,
        "created_at": format_stored_time(run.created_at),
        "elapsed_ms": elapsed,
        "elapsed_source": "trace" if trace else "recorded_timestamps",
        "result_expired": expired,
        "trace": trace,
        "source": "java",
        "gateway_version": run.gateway_version,
        "node_build": run.node_build,
        "agent_version": run.expected_agent_version if run.exec_config_confirmed else None,
    }
