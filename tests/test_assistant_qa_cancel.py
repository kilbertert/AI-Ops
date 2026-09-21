"""Stopping an assistant question: persist first, interrupt best-effort (#357).

The stop request's correctness lives in ONE write: the job row goes to the
`cancelled` terminal state, and the claim-guard is what keeps the worker's late
answer from ever overwriting it. Everything after that write — interrupting the
model turn, freeing the conversation's generation slot — is a saving, not a
precondition, and must survive failing on its own.

The model turn is therefore stubbed at the seam the runtime actually calls
(``run_zero_order_answer``) and given a live turn handle whose ``interrupt()``
the runtime is expected to reach, so these tests observe the real chain from
the stop request to the SDK's turn handle rather than a stub of it.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

QUESTION = "什么是分时电价"
ANSWER = {"text": "分时电价按峰、谷、平段分别计价。", "reminder": False}


def _runtime(tmp_path: Path):
    """A GatewayRuntime on a private temp database (mirrors the harness in
    test_assistant_qa_claim_guard.py)."""
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
    return GatewayRuntime(GatewayStore(gateway_settings.database_file), gateway_settings, Settings())


def _context():
    """The caller whose questions these are (and, for the cross-scope case,
    whose they are not)."""
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

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
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

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


def _conversation(runtime) -> dict[str, Any]:
    """A live conversation, exactly as the assistant entry point leaves it: its
    generation slot claimed by the turn this job will answer into."""
    conversation = runtime.conversation_store.create(
        scope_fingerprint=SCOPE,
        business_entry="consumer",
        agent_version_key="agt_12345678#v1",
    )
    turn_no = runtime.conversation_store.begin_turn(
        conversation["conversation_id"], SCOPE, kind="qa", question=QUESTION
    )
    return {"conversation": conversation, "turn_no": turn_no}


class _Turn:
    """Stands in for the SDK turn handle the worker runs on.

    ``interrupt()`` is the RPC the turn timeout already uses; recording the call
    is what proves the stop request reached the turn that is burning tokens,
    and ``fail`` is how a test makes that RPC fail.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True
        if self.fail:
            raise RuntimeError("interrupt RPC failed")


def _hold_the_model(
    turn: _Turn,
    *,
    at_model_call: threading.Event,
    before_turn: threading.Event,
    turn_started: threading.Event,
    release: threading.Event,
    monkeypatch,
) -> None:
    """Park the worker inside the model call, with the turn already registered.

    The registrar is the hook the runtime hands down so the live turn handle
    can be interrupted; calling it here is what a real session does the moment
    its turn starts. ``before_turn`` holds the worker between "inside the model
    call" and "a turn now exists", which is how a test reaches a stop request
    that arrives with nothing to interrupt yet.
    """
    from aiops_diagnostics import gateway_runtime

    def _model(_question, _settings, *, turn_registrar=None, **_kwargs) -> dict[str, Any]:
        at_model_call.set()
        before_turn.wait(timeout=30)
        if turn_registrar is not None:
            turn_registrar(turn)
        turn_started.set()
        release.wait(timeout=30)
        return dict(ANSWER)

    monkeypatch.setattr(gateway_runtime, "run_zero_order_answer", _model)


def _wait_for_worker(runtime, qa_id: str) -> None:
    """Block until the worker for this job has finished its follow-up writes."""
    for _ in range(500):
        if qa_id not in runtime._futures:  # noqa: SLF001 — the worker's own lifecycle
            return
        threading.Event().wait(0.02)
    raise AssertionError("the QA worker never finished")


def _gates() -> dict[str, threading.Event]:
    return {
        "at_model_call": threading.Event(),
        "before_turn": threading.Event(),
        "turn_started": threading.Event(),
        "release": threading.Event(),
    }


def _hold(gates: dict[str, threading.Event], turn: _Turn, monkeypatch) -> None:
    _hold_the_model(
        turn,
        at_model_call=gates["at_model_call"],
        before_turn=gates["before_turn"],
        turn_started=gates["turn_started"],
        release=gates["release"],
        monkeypatch=monkeypatch,
    )


def test_cancel_persists_the_terminal_state_then_interrupts_the_live_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """A stop while the model is answering: the row is terminal, the live turn
    is interrupted, and the input box is unlocked without waiting for the
    worker to notice anything."""
    runtime = _runtime(tmp_path)
    try:
        live = _conversation(runtime)
        turn = _Turn()
        gates = _gates()
        _hold(gates, turn, monkeypatch)
        gates["before_turn"].set()  # let the turn start as soon as the model runs

        qa = runtime.start_assistant_qa(
            _context(),
            QUESTION,
            conversation=live["conversation"],
            conversation_turn_no=live["turn_no"],
        )
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        cancelled = runtime.cancel_assistant_qa(_context(), qa["qa_id"])

        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
        assert cancelled["result"] is None
        assert turn.interrupted, "the stop request never reached the live turn"
        conversation_id = live["conversation"]["conversation_id"]
        assert runtime.conversation_store.get(conversation_id, SCOPE)["is_generating"] is False
        assert runtime.conversation_store.turns(conversation_id, SCOPE) == []

        # The worker's answer arrives after the cancellation: the claim-guard
        # refuses it, so nothing it produced survives — not the result, not the
        # conversation turn, not a metric row.
        gates["release"].set()
        _wait_for_worker(runtime, qa["qa_id"])
        job = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
        assert job["status"] == "cancelled"
        assert job["result"] is None
        assert runtime.conversation_store.turns(conversation_id, SCOPE) == []
        assert runtime.metrics_store.list_runs(SCOPE) == []
    finally:
        runtime.shutdown()


def test_a_failed_interrupt_still_cancels_the_job(tmp_path: Path, monkeypatch) -> None:
    """Interrupting the turn is a saving, not the contract: when the interrupt
    RPC itself fails the job is still cancelled and the slot still freed."""
    runtime = _runtime(tmp_path)
    try:
        live = _conversation(runtime)
        turn = _Turn(fail=True)
        gates = _gates()
        _hold(gates, turn, monkeypatch)
        gates["before_turn"].set()

        qa = runtime.start_assistant_qa(
            _context(),
            QUESTION,
            conversation=live["conversation"],
            conversation_turn_no=live["turn_no"],
        )
        assert gates["turn_started"].wait(10)
        cancelled = runtime.cancel_assistant_qa(_context(), qa["qa_id"])

        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
        conversation_id = live["conversation"]["conversation_id"]
        assert runtime.conversation_store.get(conversation_id, SCOPE)["is_generating"] is False
        assert runtime.conversation_store.turns(conversation_id, SCOPE) == []
        gates["release"].set()
        _wait_for_worker(runtime, qa["qa_id"])
        assert runtime.store.get_assistant_question(qa["qa_id"], SCOPE)["status"] == "cancelled"
    finally:
        runtime.shutdown()


def test_cancelling_a_job_whose_turn_has_not_started_yet(tmp_path: Path, monkeypatch) -> None:
    """The stop can land while the worker is already inside the model call but
    before any turn exists to interrupt. There is nothing to interrupt — the
    worker must then find the row terminal and leave without writing
    anything."""
    runtime = _runtime(tmp_path)
    try:
        live = _conversation(runtime)
        gates = _gates()
        _hold(gates, _Turn(), monkeypatch)

        qa = runtime.start_assistant_qa(
            _context(),
            QUESTION,
            conversation=live["conversation"],
            conversation_turn_no=live["turn_no"],
        )
        assert gates["at_model_call"].wait(10)
        cancelled = runtime.cancel_assistant_qa(_context(), qa["qa_id"])

        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
        conversation_id = live["conversation"]["conversation_id"]
        assert runtime.conversation_store.get(conversation_id, SCOPE)["is_generating"] is False
        assert runtime.conversation_store.turns(conversation_id, SCOPE) == []
        gates["before_turn"].set()
        gates["release"].set()
        _wait_for_worker(runtime, qa["qa_id"])
        job = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
        assert job["status"] == "cancelled"
        assert job["result"] is None
    finally:
        runtime.shutdown()


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
