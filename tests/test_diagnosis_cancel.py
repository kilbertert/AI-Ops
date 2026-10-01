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
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_assistant_api import _Authorizer, _Caller, _Directory, _Turn

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
        raise RuntimeError("the stop must have happened before the model returned")

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

    This is the same rule the read path already follows: a caller learns
    nothing about whether a diagnosis exists outside its scope.
    """
    gates = _gates()
    client, runtime = _diagnosis_client(tmp_path, monkeypatch, gates=gates, turn=_Turn())
    try:
        missing = client.post(
            "/v1/standard/diagnoses/dx_nonexistent00000000000000000000001/cancel", headers=HEADERS
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
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
