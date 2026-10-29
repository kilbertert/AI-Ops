"""现役问答任务的重复取消、权限与保留期。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

QUESTION = "什么是分时电价"
ANSWER = {"text": "分时电价按峰、谷、平段分别计价。", "reminder": False}


def _runtime(tmp_path: Path):
    """Current task store on a private temporary root."""
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore

    os.chmod(tmp_path, 0o750)
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    os.chmod(config, 0o600)
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    return GatewayRuntime(
        GatewayStore(gateway_settings.database_file),
        gateway_settings,
        Settings(),
    )


def _context():
    """The caller whose questions these are (and, for the cross-scope case,
    whose they are not)."""
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _other_context():
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="c:C-2", c_user_id="C-2", tenant_id="T-2")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-2",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


#: The caller's scope fingerprint — the same value the store keys jobs and
#: conversations on, so a job created for this caller is the one this caller
#: can stop.
SCOPE = _context().scope_fingerprint
#: The tenant the metrics rows are keyed on. They are tenant-scoped and never
#: scope-scoped, so assertions about them read this tenant rather than the
#: fingerprint above.
TENANT = "T-1"


def test_repeated_and_late_cancels_report_the_jobs_own_state(tmp_path: Path) -> None:
    """The stop button is pressed twice under a flaky network, and sometimes a
    moment after the answer already landed. Neither is an error: the caller is
    told the job's current state either way."""
    runtime = _runtime(tmp_path)
    try:
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
        first = runtime.cancel_assistant_qa(_context(), qa["qa_id"])
        assert first is not None and first["status"] == "cancelled"
        again = runtime.cancel_assistant_qa(_context(), qa["qa_id"])
        assert again is not None and again["status"] == "cancelled"

        # A job that completed while the user was reaching for the button keeps
        # its answer: cancelling it is a no-op, never a rewrite.
        answered = runtime.store.create_assistant_question(SCOPE, QUESTION)
        runtime.store.update_assistant_question(answered["qa_id"], status="completed", result=ANSWER)
        late = runtime.cancel_assistant_qa(_context(), answered["qa_id"])
        assert late is not None
        assert late["status"] == "completed"
        assert late["result"] == ANSWER
    finally:
        runtime.shutdown()


def test_cancelling_another_scopes_job_is_indistinguishable_from_missing(tmp_path: Path) -> None:
    """Cancelling must not become a probe: another scope's job and a job that
    never existed both come back as nothing at all, and the job is untouched."""
    runtime = _runtime(tmp_path)
    try:
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
        runtime.store.update_assistant_question(qa["qa_id"], status="running")

        assert runtime.cancel_assistant_qa(_other_context(), qa["qa_id"]) is None
        assert runtime.cancel_assistant_qa(_context(), "qa_doesnotexist00000000000000000000") is None
        assert runtime.store.get_assistant_question(qa["qa_id"], SCOPE)["status"] == "running"
    finally:
        runtime.shutdown()


def test_a_cancelled_question_outlives_its_refresh_window(tmp_path: Path) -> None:
    """The row is kept on the short (failed) retention tier: the user is still
    looking at 「已停止」 when they refresh, so the job must still be there —
    and must not be resurrectable by a late worker afterwards."""
    runtime = _runtime(tmp_path)
    try:
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
        runtime.cancel_assistant_qa(_context(), qa["qa_id"])
        with runtime.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE assistant_questions SET expires_at = ? WHERE qa_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), qa["qa_id"]),
            )
        assert runtime.store.get_assistant_question(qa["qa_id"], SCOPE)["status"] == "expired"
    finally:
        runtime.shutdown()
