"""A refused terminal write must stop the assistant-question worker.

PRD #346 / #355: the runtime wrote every assistant-question terminal state
without looking at what the claim-guard returned, so a job whose row another
path had already driven terminal still received its full follow-up treatment —
the answer persisted into the conversation turn while the job row itself
carried none. Expiry produces that race today; the cancel endpoint (#357) is
the path that would make it routine instead of rare.

The guard: a terminal write that comes back False ends the worker right there
— no conversation turn, no metric — because nothing the worker produced was
persisted. Refusal is a legal race, not an error, so the worker exits quietly.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCOPE = "tenant-a"
QUESTION = "什么是分时电价"
ANSWER = {"text": "分时电价按峰、谷、平段分别计价。", "reminder": False}


def _runtime(tmp_path: Path):
    """A GatewayRuntime on a private temp database (mirrors the harness in
    test_promo_unavailable_runtime.py)."""
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


def _conversation_turn(runtime) -> tuple[str, int]:
    """A live conversation with its generation slot claimed, exactly as the
    assistant entry point leaves it before the worker starts."""
    conversation = runtime.conversation_store.create(
        scope_fingerprint=SCOPE,
        business_entry="consumer",
        agent_version_key="agt_12345678#v1",
    )
    turn_no = runtime.conversation_store.begin_turn(
        conversation["conversation_id"], SCOPE, kind="qa", question=QUESTION
    )
    return conversation["conversation_id"], turn_no


def _push_past_deadline(runtime, qa_id: str) -> None:
    """The job's deadline passes while the worker is still answering, so the
    next store write sweeps the row to `expired` first."""
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with runtime.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE assistant_questions SET deadline_at = ? WHERE qa_id = ?",
            (past, qa_id),
        )


def _plain_answer(_question, _settings, **_kwargs) -> dict[str, Any]:
    return dict(ANSWER)


def _run_worker(runtime, qa_id: str, conversation_turn: tuple[str, int], monkeypatch, answer=None) -> None:
    """Drive the QA worker synchronously, as its executor thread would.

    ``answer`` stands in for the model call — that hook is also how a test
    makes the job's deadline pass while the worker is still answering.
    """
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_zero_order_answer", answer or _plain_answer)
    runtime._execute_assistant_qa(
        qa_id,
        None,
        QUESTION,
        None,
        None,
        SCOPE,
        conversation_turn,
        "zh",
        None,
        None,
    )


def test_expired_job_keeps_its_terminal_row_and_drops_the_late_answer(tmp_path: Path, monkeypatch) -> None:
    """The worker loses the race: expiry lands mid-flight, so its terminal
    write is refused. Nothing it produced may survive — not the result, not
    the conversation turn, not the metric row."""
    runtime = _runtime(tmp_path)
    try:
        conversation_id, turn_no = _conversation_turn(runtime)
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)

        def answer(_question, _settings, **_kwargs) -> dict[str, Any]:
            _push_past_deadline(runtime, qa["qa_id"])
            return dict(ANSWER)

        _run_worker(runtime, qa["qa_id"], (conversation_id, SCOPE, turn_no), monkeypatch, answer)
    finally:
        runtime.shutdown()

    job = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert job["status"] == "expired"
    assert job["result"] is None
    # The turn row is dropped: an interrupted generation never survives as a
    # complete reply, and the slot is released.
    assert runtime.conversation_store.turns(conversation_id, SCOPE) == []
    assert not runtime.conversation_store.get(conversation_id, SCOPE)["is_generating"]
    assert runtime.metrics_store.list_runs(SCOPE) == []


def test_a_terminal_write_that_lands_still_persists_result_turn_and_metric(
    tmp_path: Path, monkeypatch
) -> None:
    """Over-correction guard: a write the claim-guard accepts keeps its full
    follow-up treatment, so checking the return value costs the happy path
    nothing."""
    runtime = _runtime(tmp_path)
    try:
        conversation_id, turn_no = _conversation_turn(runtime)
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
        _run_worker(runtime, qa["qa_id"], (conversation_id, SCOPE, turn_no), monkeypatch)
    finally:
        runtime.shutdown()

    job = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert job["status"] == "completed"
    assert job["result"] == ANSWER
    turns = runtime.conversation_store.turns(conversation_id, SCOPE)
    assert [turn["answer"] for turn in turns] == [ANSWER]
    assert len(runtime.metrics_store.list_runs(SCOPE)) == 1


def test_rag_path_refusal_stops_the_worker(tmp_path: Path, monkeypatch) -> None:
    """Seam-level contract guard: the RAG path reports a refused write with
    ``TERMINAL_WRITE_REFUSED``, and the worker must treat that as "stop".

    Without the branch the marker would fall into the completed arm, where it
    is a dict like any other result: a bogus completed metric plus a
    conversation turn holding ``{"status": "refused"}`` as its answer. The RAG
    path itself needs a published agent plus kb-service to reach, so this pins
    the caller's side of the contract and leaves the path's own coverage to
    test_promo_unavailable_runtime.py.
    """
    from aiops_diagnostics.gateway_runtime import TERMINAL_WRITE_REFUSED

    runtime = _runtime(tmp_path)
    try:
        conversation_id, turn_no = _conversation_turn(runtime)
        qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
        # Only the capability check gates the branch, and the RAG path is
        # stubbed out below, so bare stand-ins are enough to route into it.
        runtime.kb_search_client = object()
        runtime.media_signer = object()
        monkeypatch.setattr(runtime, "_try_customer_rag", lambda *a, **k: TERMINAL_WRITE_REFUSED)
        _run_worker(runtime, qa["qa_id"], (conversation_id, SCOPE, turn_no), monkeypatch)
    finally:
        runtime.shutdown()

    # The row keeps the `running` claim this worker made and nothing more: no
    # terminal state, no metric, no turn.
    assert runtime.store.get_assistant_question(qa["qa_id"], SCOPE)["status"] == "running"
    assert runtime.store.get_assistant_question(qa["qa_id"], SCOPE)["result"] is None
    assert runtime.conversation_store.turns(conversation_id, SCOPE) == []
    assert not runtime.conversation_store.get(conversation_id, SCOPE)["is_generating"]
    assert runtime.metrics_store.list_runs(SCOPE) == []
