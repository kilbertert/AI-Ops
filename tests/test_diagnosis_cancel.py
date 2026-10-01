"""#499: the order-diagnosis line can be stopped, and its turn really is interrupted.

The stop button used to exist only on the general-question line. A diagnosis
could hold the conversation's generation slot for up to fifteen minutes with no
way to let go short of leaving the conversation entirely, and the handoff told
two outside teams to render a waiting state with no way out.

These tests run the **real** runtime (real ``GatewayRuntime``, real
``GatewayStore``, real HTTP routes) because everything worth checking here
happens inside a worker that a stub would replace: whether the terminal row was
written, whether the live model turn was actually reached, and whether the
conversation slot came free. A stub runtime would prove the stub.

The model turn is held inside the patched diagnosis call while the stop lands —
which is the state a stop request actually arrives in: the worker is running,
the slot is taken, and no result has been written yet.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_assistant_api import _Authorizer, _Caller, _Directory, _Turn

from aiops_diagnostics.codex_runtime import AgentRuntimeError
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

ORDER = "2096164064667852801"
HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


def _diagnosis_client(tmp_path: Path, monkeypatch, *, gates: dict, turn: _Turn):
    """The real stack, with the diagnosis worker parked inside its model call."""
    from aiops_diagnostics import gateway_runtime
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)

    monkeypatch.setattr(GatewayRuntime, "classify_lightweight", lambda *a, **k: None)

    def _diagnose(*_args: Any, turn_registrar=None, **_kwargs: Any):
        gates["at_model_call"].set()
        gates["before_turn"].wait(timeout=30)
        if turn_registrar is not None:
            turn_registrar(turn)
        gates["turn_started"].set()
        gates["release"].wait(timeout=30)
        # AgentRuntimeError, not RuntimeError: the worker only handles the
        # former, and a bare RuntimeError would escape before its failed branch
        # ever ran — which is what made the metric guard look untestable.
        raise AgentRuntimeError("the interrupted turn surfaced as a runtime error")

    monkeypatch.setattr(gateway_runtime, "run_agent_diagnosis", _diagnose)

    runtime = GatewayRuntime(GatewayStore(settings.database_file), settings, Settings())
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,
        caller_resolver=_Caller(),
        order_authorizer=_Authorizer({ORDER}),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), runtime


def _gates() -> dict:
    return {
        "at_model_call": threading.Event(),
        "before_turn": threading.Event(),
        "turn_started": threading.Event(),
        "release": threading.Event(),
    }


def test_stopping_a_diagnosis_frees_the_slot_and_interrupts_the_turn(tmp_path: Path, monkeypatch) -> None:
    """The stop terminalises the job, frees the conversation, and reaches the turn.

    The three things a stop has to do, in the order the contract states them:
    write the terminal row (that alone makes the cancellation real), free the
    generation slot without waiting for the worker, and interrupt the live turn
    (a saving, not the contract).
    """
    turn = _Turn()
    gates = _gates()
    gates["before_turn"].set()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=turn)
    try:
        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=HEADERS,
        ).json()
        cid = conversation["conversation_id"]

        started = client.post(
            f"/v1/conversations/{cid}/active-order", json={"order_no": ORDER}, headers=HEADERS
        )
        assert started.status_code == 200, started.text

        asked = client.post(
            "/v1/assistant/questions",
            json={"question": "这个订单为什么提前停了", "conversation_id": cid},
            headers=HEADERS,
        )
        assert asked.status_code == 202, asked.text
        assert asked.json()["type"] == "diagnosis", asked.text
        diagnosis_id = asked.json()["diagnosis_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        stopped = client.post(f"/v1/standard/diagnoses/{diagnosis_id}/cancel", headers=HEADERS)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "cancelled"
        # The stop response IS the terminal state: no second poll needed.
        assert stopped.json()["result"] is None

        # Polling afterwards is stable, not a race.
        polled = client.get(f"/v1/standard/diagnoses/{diagnosis_id}", headers=HEADERS)
        assert polled.json()["status"] == "cancelled"

        # The generation slot is free at once — no 409 on the next question.
        detail = client.get(f"/v1/conversations/{cid}", headers=HEADERS)
        assert detail.json()["is_generating"] is False

        assert turn.interrupted, "the stop never reached the live model turn"
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def test_cancelling_a_diagnosis_the_caller_cannot_see_is_not_found(tmp_path: Path, monkeypatch) -> None:
    """Missing and out-of-scope are one answer — cancelling must not probe ids.

    Two halves, and both are needed: an id that does not exist, and a real id
    belonging to **another caller**. The first half alone is what review caught
    — it never created a diagnosis owned by somebody else, so it proved nothing
    about the scope check, which is the half that actually guards the data. The
    two responses must be indistinguishable, or the endpoint becomes a way to
    learn which diagnosis ids exist.
    """
    gates = _gates()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    try:
        scope = _scope_fingerprint(client)
        # A real, in-flight diagnosis that belongs to a DIFFERENT caller.
        # Written straight into the store under another scope: the cancel path
        # only reads the row by (id, scope), so this is the whole setup it needs.
        theirs = runtime.store.create_standard_diagnosis("other-scope-fingerprint", ORDER, "别人的问题", None)

        missing = client.post(
            "/v1/standard/diagnoses/dx_nonexistent00000000000000000000001/cancel", headers=HEADERS
        )
        someone_elses = client.post(
            f"/v1/standard/diagnoses/{theirs['diagnosis_id']}/cancel", headers=HEADERS
        )

        assert missing.status_code == someone_elses.status_code == 404
        assert missing.json() == someone_elses.json(), (
            "an existing-but-not-mine diagnosis answered differently from a missing one"
        )
        # And the other caller's job was not touched.
        assert (
            runtime.store.get_standard_diagnosis(theirs["diagnosis_id"], "other-scope-fingerprint")["status"]
            == "queued"
        )
        assert scope  # the caller's own scope is not the one above
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def test_cancelling_a_finished_diagnosis_returns_its_own_terminal_state(tmp_path: Path, monkeypatch) -> None:
    """A stop that loses the race answers the job's own state, never an error.

    The stop button is pressed under flaky networks, so a repeat click or a
    cancel that arrives just after completion must not read as a failure — and
    must not rewrite a completed result into ``cancelled``.
    """
    gates = _gates()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    try:
        scope = _scope_fingerprint(client)
        # A diagnosis the store already holds as completed, with no worker run
        # at all: the cancel path only ever reads the row it finds, so this is
        # the whole setup it needs.
        created = runtime.store.create_standard_diagnosis(scope, ORDER, "历史问题", None)
        assert runtime.store.update_standard_diagnosis(
            created["diagnosis_id"], status="completed", result={"summary": "已完成的结论"}
        )

        again = client.post(f"/v1/standard/diagnoses/{created['diagnosis_id']}/cancel", headers=HEADERS)
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "completed", "a finished diagnosis must not become cancelled"
        assert again.json()["result"] == {"summary": "已完成的结论"}
        # 终态行没有被改写 —— 这一行是**下层的**保证（store 的 UPDATE 带
        # WHERE status IN ('queued','running')），不是 runtime 那个 if 的功劳。
        # 两个都写在这里：runtime 的分支避免了无谓的写，store 的谓词才是最终裁判。
        assert runtime.store.get_standard_diagnosis(created["diagnosis_id"], scope)["status"] == "completed"
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def _scope_fingerprint(client: TestClient) -> str:
    """Resolve the caller once through the app's own resolver to learn its scope.

    Read rather than hardcoded: a fingerprint literal in a test is a second
    definition of the scope rule, and it would silently stop matching the
    moment that rule changed.
    """
    resolver = client.app.state.gateway.caller_resolver
    context = resolver.resolve("service", required_scope="aiops:diagnoses:write", third_session=None)
    return context.scope_fingerprint


def test_a_cancelled_diagnosis_turn_stays_out_of_the_context_window(tmp_path: Path, monkeypatch) -> None:
    """Acceptance criterion 3: the stopped turn does not become history.

    A question the user cancelled is one they rejected — it must not come back
    as context for the next one. The mechanism is the claim guard: the worker's
    late ``_complete_conversation_turn(..., cancelled=True, guarded=True)``
    finds the row already terminal and drops the turn instead of filling it.

    Asserted through the store the app itself uses, so this is the same window
    the next question reads.
    """
    gates = _gates()
    gates["before_turn"].set()  # let the turn start as soon as the model runs
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    try:
        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=HEADERS,
        ).json()
        cid = conversation["conversation_id"]
        client.post(f"/v1/conversations/{cid}/active-order", json={"order_no": ORDER}, headers=HEADERS)

        asked = client.post(
            "/v1/assistant/questions",
            json={"question": "这个订单为什么提前停了", "conversation_id": cid},
            headers=HEADERS,
        )
        diagnosis_id = asked.json()["diagnosis_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        stopped = client.post(f"/v1/standard/diagnoses/{diagnosis_id}/cancel", headers=HEADERS)
        assert stopped.json()["status"] == "cancelled"

        scope = _scope_fingerprint(client)
        store = client.app.state.gateway.conversation_store
        # The cancelled turn never becomes history: the window is empty.
        assert store.context_turns(cid, scope) == [], (
            "a cancelled turn entered the context window — the user rejected that answer"
        )
        # The turn row itself is not left behind holding the slot either.
        detail = client.get(f"/v1/conversations/{cid}", headers=HEADERS).json()
        assert detail["turns"] == []
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def test_a_cancel_that_loses_the_race_does_not_delete_the_workers_turn(tmp_path: Path, monkeypatch) -> None:
    """A stop that loses the terminal write must not free the turn slot.

    ``release_turn`` DELETES the turn row, so a cancel that lost the race would
    delete a turn the worker had already filled (or is about to fill) — the
    caller sees the job's own state and its own history silently damaged. The
    guard is ``if accepted:`` around the release; this drives that branch by
    making the store's terminal write lose, with the turn row genuinely present.
    """
    gates = _gates()
    gates["before_turn"].set()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    try:
        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=HEADERS,
        ).json()
        cid = conversation["conversation_id"]
        client.post(f"/v1/conversations/{cid}/active-order", json={"order_no": ORDER}, headers=HEADERS)

        asked = client.post(
            "/v1/assistant/questions",
            json={"question": "这个订单为什么提前停了", "conversation_id": cid},
            headers=HEADERS,
        )
        diagnosis_id = asked.json()["diagnosis_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        scope = _scope_fingerprint(client)
        store = client.app.state.gateway.conversation_store
        assert store.turns(cid, scope), "the turn row should exist while the job runs"

        # The worker finishes and fills the turn — the state a losing cancel
        # actually meets. Without this the assertion below would pass on an
        # empty row and prove only that an unfinished row survived (review).
        store.complete_turn(cid, scope, asked.json()["turn_no"], answer={"text": "已经算好的结论"})

        # The losing write: another path terminalised the row first, so this
        # cancel's UPDATE matches nothing.
        monkeypatch.setattr(runtime.store, "update_standard_diagnosis", lambda *a, **k: False)
        stopped = client.post(f"/v1/standard/diagnoses/{diagnosis_id}/cancel", headers=HEADERS)
        assert stopped.status_code == 200

        survived = store.turns(cid, scope)
        assert survived, "a losing cancel released the slot and deleted the worker's turn row"
        assert survived[0]["answer"] == {"text": "已经算好的结论"}, (
            "a losing cancel destroyed the answer the worker had already written"
        )
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def test_a_turn_that_starts_after_the_stop_is_still_interrupted(tmp_path: Path, monkeypatch) -> None:
    """A stop that ran before the worker reached its turn must still stop it.

    The stop pops the registration and finds no handle — but the worker still
    holds that very object and will register into it moments later. Without the
    ``cancelled`` flag the turn runs to completion on a job already reported as
    ``cancelled``, and the interrupt nobody will look at again never happens.
    """
    from aiops_diagnostics.gateway_runtime import JobRegistration

    turn = _Turn()
    registration = JobRegistration(conversation_turn=("conv_x", "scope", 1), route_type="diagnosis")
    registration.cancelled = True  # what the stop does while the worker spins up

    registration.register_interrupt(turn)

    assert turn.interrupted, "a turn starting after the stop was not interrupted"
    assert registration.interrupt is None, "a stopped job should not adopt a new handle"


def test_a_cancelled_diagnosis_is_not_also_counted_as_failed(tmp_path: Path, monkeypatch) -> None:
    """Stopping a job must not inflate the failure rate (review on #499).

    The interrupt makes the worker's model call raise, so the worker lands in
    its ``failed`` branch for a job the user deliberately stopped. The store's
    terminal guard refuses that write — but the metric row is not the store's to
    refuse, so it was recorded anyway: one stop, two rows, one of them wrong.
    The guard is ``if not ok: return`` before the metric.
    """
    gates = _gates()
    gates["before_turn"].set()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    recorded: list[dict] = []
    try:
        original = runtime.metrics_store.record
        monkeypatch.setattr(
            runtime.metrics_store,
            "record",
            lambda **fields: (recorded.append(fields), original(**fields))[1],
        )

        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=HEADERS,
        ).json()
        cid = conversation["conversation_id"]
        client.post(f"/v1/conversations/{cid}/active-order", json={"order_no": ORDER}, headers=HEADERS)
        asked = client.post(
            "/v1/assistant/questions",
            json={"question": "这个订单为什么提前停了", "conversation_id": cid},
            headers=HEADERS,
        )
        diagnosis_id = asked.json()["diagnosis_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        client.post(f"/v1/standard/diagnoses/{diagnosis_id}/cancel", headers=HEADERS)

        # Let the interrupted worker finish its failed branch.
        gates["release"].set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(row.get("outcome") == "failed" for row in recorded):
            time.sleep(0.05)

        outcomes = [row.get("outcome") for row in recorded if row.get("route_type") == "diagnosis"]
        assert "failed" not in outcomes, f"a cancelled diagnosis was also counted as failed: {outcomes}"
        assert "cancelled" in outcomes, f"the stop itself must still be counted: {outcomes}"
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()
