"""现有运行账本的只读元数据投影，不恢复业务结果或推进过期状态。"""

from datetime import UTC, datetime

from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.operator import operator_tools

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
        runs = [_run_from_row(row) for row in rows[:limit]]
        # 问题全文在同库 assistant_questions，沿用同一只读连接与同一指纹判据——
        # 不走 get_assistant_question：它顺带做过期收敛，是一次写。
        qa_ids = [run.qa_id for run in runs if run.qa_id]
        questions = {}
        if qa_ids:
            marks = ", ".join("?" for _ in qa_ids)
            for row in connection.execute(
                "SELECT qa_id, question FROM assistant_questions "
                f"WHERE scope_fingerprint = ? AND qa_id IN ({marks})",
                (caller.scope_fingerprint, *qa_ids),
            ).fetchall():
                questions[row["qa_id"]] = row["question"]
    return [metadata(run, question=questions.get(run.qa_id)) for run in runs], len(rows) > limit


def metadata(run, *, question=None):
    # 投影运行事实与已批准的业务标识（问题、订单号）；答案、证据 data、工具
    # 参数、凭据与权限指纹仍不投影，过期记录不通过这里回读业务结果。
    trace = [
        {
            key: event[key]
            for key in ("phase", "elapsed_ms", "duration_ms", "at", "tool", "status", "detail")
            if key in event
        }
        for event in getattr(run, "trace", ())[-128:]
    ]
    elapsed = trace[-1]["elapsed_ms"] if trace else None
    if elapsed is None and run.completed_at is not None:
        elapsed = max(0, int((run.completed_at - run.created_at).total_seconds() * 1000))
    expired = run.expires_at <= datetime.now(UTC)
    snapshot = run.snapshot if isinstance(run.snapshot, dict) else {}
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
    receipt = run.receipt()
    evidence = snapshot.get("evidence")
    history = snapshot.get("history_runs")
    # 死 run 取证路径里证据键名来自节点回调（operator_cpms 终局吸收），不是
    # 本进程校验过的常量——只投影仍在声明集里的工具名，注入文本按键滤掉。
    declared = set(operator_tools.names())
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
        # ADR-0249 扩入：终止原因只投影账本里的机器码（拒绝/失败/撤销码），
        # 不投影自由文本的失败原文——账本这一列本来就是码。上限按最长合法
        # 组合 `operator_node_failed: tool_refused:<80>`（115 字符）留足。
        "reason": str(run.reason)[:128] if run.reason else None,
        "question": question,
        "order_no": run.order_no or None,
        # ADR-0251：行级展示摘要（入参 + 白名单结果字段），仍走同一 scope 闸。
        "tool_summaries": run.tool_summaries or None,
        "language": run.language,
        "environment": receipt["expected_environment"],
        "expected_node_id": receipt["expected_node_id"],
        "actual_node_id": receipt["actual_node_id"],
        "agent_version_observed": receipt.get("agent_version_observed"),
        "agent_version_gap": receipt["agent_version_gap"],
        "claimed_at": receipt["claimed_at"],
        "completed_at": receipt["completed_at"],
        "expires_at": receipt["expires_at"],
        # 结果指纹用来对照「交付的是不是这条快照」，不还原结果本身。
        "result_sha256": receipt["result_sha256"],
        # 失败与部分快照不写 trial 键；权威判据与 operator_cpms:294 一致——
        # 试用会话的 chat_binding 恒为 `operator-trial:` 前缀。绑定本身不投影：
        # 试用形态的绑定串内部标识（operator-trial:<app uuid>），不是排障信号。
        "trial": bool(snapshot.get("trial")) or str(run.chat_binding or "").startswith("operator-trial:"),
        "history_run_count": len(history) if isinstance(history, list) else 0,
        # 只投影产生了证据的声明内工具名，不投影证据 data。
        "evidence_tools": sorted(key for key in evidence if key in declared)[:16]
        if isinstance(evidence, dict)
        else [],
    }
