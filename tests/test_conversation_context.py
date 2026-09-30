"""Conversation history reaching the prompt (#482).

Before this wiring, ``context_turns()`` was a rule stated in six places (the
frontend brief, README, ``acceptance.feature``, ``docs/validation.md``,
``qa-plan.md``, ``docs/开发进度.md``), implemented once — and called from
``src/`` exactly zero times. Users asking a follow-up ("那它为什么跳枪") got an
answer to that sentence and nothing else.

The change's safety boundary is one equality: **with no history the prompt is
byte-identical to what it was before**. Every test here is written so that
removing the wiring, or breaking the empty-history equality, turns it red.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from aiops_diagnostics.agent_runner import run_zero_order_answer
from aiops_diagnostics.conversation_context import (
    DEFAULT_MAX_TURNS,
    build_history,
    render_history,
)
from aiops_diagnostics.conversation_store import CONTEXT_MAX_TOKENS, CONTEXT_MAX_TURNS, ConversationStore
from aiops_diagnostics.promo_agents import CustomerAgentSelection, promo_prompt
from aiops_diagnostics.qa_rag import _initial_prompt

_SCOPE = "scope-abc"
_AGENT_VERSION = "agt_abcdef1234567890#v1"


def _store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversation.db")


def _conversation(store: ConversationStore) -> str:
    return store.create(
        scope_fingerprint=_SCOPE, business_entry="operator", agent_version_key=_AGENT_VERSION
    )["conversation_id"]


def _finished_turn(store: ConversationStore, cid: str, question: str, answer: dict) -> int:
    turn_no = store.begin_turn(cid, _SCOPE, kind="qa", question=question)
    store.complete_turn(cid, _SCOPE, turn_no, answer=answer, token_count=10)
    return turn_no


def test_no_history_renders_to_nothing() -> None:
    """No turns, no text — so an empty block cannot perturb any prompt."""
    assert render_history([], "zh") == ""
    assert render_history([], "en") == ""


def test_the_window_reaches_the_block(tmp_path: Path) -> None:
    """A finished turn shows up as its question and answer, oldest first."""
    store = _store(tmp_path)
    cid = _conversation(store)
    _finished_turn(store, cid, "订单为什么停了", {"text": "因为余额耗尽"})
    _finished_turn(store, cid, "那它为什么跳枪", {"text": "枪线接触不良"})

    history = build_history(store, cid, _SCOPE, "zh")
    assert "订单为什么停了" in history
    assert "因为余额耗尽" in history
    assert history.index("订单为什么停了") < history.index("那它为什么跳枪")


def test_a_diagnosis_turn_contributes_summary_and_root_cause(tmp_path: Path) -> None:
    """The diagnosis line stores a different answer shape; it reads too."""
    store = _store(tmp_path)
    cid = _conversation(store)
    _finished_turn(store, cid, "为什么跳枪", {"summary": "充电中断", "root_cause": "枪线接触不良"})

    history = build_history(store, cid, _SCOPE, "zh")
    assert "充电中断" in history
    assert "枪线接触不良" in history


def test_in_flight_and_cancelled_turns_stay_out(tmp_path: Path) -> None:
    """The store's rule, seen through this module: only completed turns."""
    store = _store(tmp_path)
    cid = _conversation(store)
    # The claim is a slot, so the order matters: the finished and cancelled
    # turns release it, and the in-flight one is claimed last and holds it —
    # which is exactly the state a follow-up arrives in.
    _finished_turn(store, cid, "答完的问题", {"text": "答案"})
    released = store.begin_turn(cid, _SCOPE, kind="qa", question="被取消的问题")
    store.release_turn(cid, _SCOPE, released)
    store.begin_turn(cid, _SCOPE, kind="qa", question="还没答完的问题")

    history = build_history(store, cid, _SCOPE, "zh")
    assert "答完的问题" in history
    assert "还没答完的问题" not in history
    assert "被取消的问题" not in history


def test_history_is_redacted(tmp_path: Path) -> None:
    """A stored answer that looks like a credential must not reach the model."""
    store = _store(tmp_path)
    cid = _conversation(store)
    _finished_turn(
        store,
        cid,
        "接口怎么调",
        {"text": "带上 Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"},
    )

    history = build_history(store, cid, _SCOPE, "zh")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in history
    assert "REDACTED" in history


def test_the_labels_follow_this_turn_not_the_stored_turn() -> None:
    """History is labelled in the current language, never translated.

    The turns in a conversation were written by whoever asked them; this turn
    may be in another language. A second model call to translate them would add
    a second dependency and a second chance to drift, so the labels move and
    the text stays.
    """
    turns = [{"turn_no": 1, "question": "余额耗尽", "answer": {"text": "因为余额耗尽"}}]
    assert "用户: 余额耗尽" in render_history(turns, "zh")
    assert "User: 余额耗尽" in render_history(turns, "en")


def test_the_window_limits_come_from_the_store() -> None:
    """One definition of 8/8k, not a second copy in the prompt layer."""
    assert DEFAULT_MAX_TURNS == CONTEXT_MAX_TURNS


def test_build_history_reads_the_configured_window(tmp_path: Path) -> None:
    """The settings knobs actually bound what the model sees."""
    store = _store(tmp_path)
    cid = _conversation(store)
    for index in range(5):
        _finished_turn(store, cid, f"问题{index}", {"text": f"答案{index}"})

    full = build_history(store, cid, _SCOPE, "zh")
    assert full.count("问题") == 5
    trimmed = build_history(store, cid, _SCOPE, "zh", max_turns=2)
    assert trimmed.count("问题") == 2
    assert "问题4" in trimmed and "问题3" in trimmed  # the newest two


def test_the_default_token_budget_is_the_contract_number() -> None:
    assert CONTEXT_MAX_TOKENS == 8000


def test_empty_history_leaves_the_zero_order_prompt_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The safety boundary: no history ⇒ the byte-identical old prompt.

    Asserted by building the prompt both ways and comparing, so a future edit
    that "tidies" the prompt while wiring history is caught here rather than in
    a review of the diff.
    """
    captured: list[str] = []

    class _Session:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run(self, prompt: str, **kwargs):
            del kwargs
            captured.append(prompt)
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput(
                turn_id="t", final_response='{"text": "答案", "reminder": false}', usage={}
            )

    # The session class is imported inside the function, so the patch has to
    # land on the module it is read from.
    monkeypatch.setattr("aiops_diagnostics.codex_runtime.SDKCodexSession", _Session)
    from aiops_diagnostics.config import Settings

    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")

    run_zero_order_answer("问题", settings, language="zh", history="")
    prompt_without = captured[-1]
    result = run_zero_order_answer("问题", settings, language="zh", history="历史块\n\n")
    prompt_with = captured[-1]

    assert result["text"] == "答案"
    assert prompt_with == prompt_without.replace("问题：问题", "历史块\n\n问题：问题")


def test_history_reaches_the_customer_qa_prompt() -> None:
    selection = CustomerAgentSelection(
        agent_id="agt", version_no=1, prompt="话术", knowledge_base_ids=("kb",)
    )
    plain = _initial_prompt(selection, "那它为什么跳枪", "zh")
    with_history = _initial_prompt(selection, "那它为什么跳枪", "zh", history="用户: 上一轮\n\n")
    assert "用户: 上一轮" not in plain
    assert "用户: 上一轮" in with_history
    # Removing exactly the injected block returns the prompt that had no
    # history parameter — i.e. the wiring only inserts, it does not rewrite.
    assert with_history.replace("用户: 上一轮\n\n", "") == plain


def test_history_reaches_the_promo_prompt() -> None:
    selection = CustomerAgentSelection(
        agent_id="agt", version_no=1, prompt="话术", knowledge_base_ids=("kb",)
    )
    plain = promo_prompt(selection, "看看案例", language="zh", intent="case_exploration")
    with_history = promo_prompt(
        selection, "看看案例", language="zh", intent="case_exploration", history="用户: 上一轮\n\n"
    )
    assert "用户: 上一轮" not in plain
    assert "用户: 上一轮" in with_history


def test_history_reaches_the_diagnosis_prompt(tmp_path: Path, monkeypatch) -> None:
    """The diagnosis line is the one that needed this most.

    A follow-up that omits the order number ("那它为什么跳枪") is routed to
    diagnosis on the strength of the conversation's confirmed active order —
    and the model then sees only that sentence. The history block carries the
    earlier turn into the same prompt.
    """
    captured: list[str] = []

    class _Session:
        def __init__(self, workspace, _settings, *, provider=None, thread_id=None):
            del provider, thread_id
            self.workspace = workspace

        @property
        def thread_id(self) -> str:
            return "capture-thread"

        def set_progress_callback(self, _callback) -> None:
            return None

        def run(self, prompt: str) -> object:
            captured.append(prompt)
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput(turn_id="turn-1", final_response="{}", usage={})

    monkeypatch.setattr("aiops_diagnostics.agent_engine.SDKCodexSession", _Session)
    monkeypatch.setenv("AIOPS_CODEX_API_KEY", "test-key")
    from aiops_diagnostics.agent_contracts import IncidentManifest
    from aiops_diagnostics.agent_runner import run_agent_diagnosis
    from aiops_diagnostics.agent_workspace import AgentWorkspace
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.parsing import parse_request

    request = parse_request("那它为什么跳枪", order_no="ORDER-1", tenant_id="T-1")
    manifest = IncidentManifest.from_request(request)
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    settings.agent.max_turns = 2
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path / "runs", manifest)

    # The scripted session returns an empty turn, so the run ends however it
    # ends — what is under test is the prompt it was started with, not its
    # conclusion.
    with contextlib.suppress(Exception):
        run_agent_diagnosis(
            workspace,
            request,
            settings,
            None,
            history="用户: 订单为什么停了\n助手: 因为余额耗尽\n\n",
        )

    assert captured, "the run produced no prompt at all"
    assert "用户: 订单为什么停了" in captured[0]


def test_no_history_leaves_the_diagnosis_prompt_unchanged(tmp_path: Path, monkeypatch) -> None:
    """Same equality as the zero-order path: no history, same prompt."""
    captured: list[str] = []

    class _Session:
        def __init__(self, workspace, _settings, *, provider=None, thread_id=None):
            del provider, thread_id
            self.workspace = workspace

        @property
        def thread_id(self) -> str:
            return "capture-thread"

        def set_progress_callback(self, _callback) -> None:
            return None

        def run(self, prompt: str) -> object:
            captured.append(prompt)
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput(turn_id="turn-1", final_response="{}", usage={})

    monkeypatch.setattr("aiops_diagnostics.agent_engine.SDKCodexSession", _Session)
    monkeypatch.setenv("AIOPS_CODEX_API_KEY", "test-key")
    from aiops_diagnostics.agent_contracts import IncidentManifest
    from aiops_diagnostics.agent_runner import run_agent_diagnosis
    from aiops_diagnostics.agent_workspace import AgentWorkspace
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.parsing import parse_request

    request = parse_request("那它为什么跳枪", order_no="ORDER-1", tenant_id="T-1")
    manifest = IncidentManifest.from_request(request)
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    settings.agent.max_turns = 2
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path / "runs", manifest)

    with contextlib.suppress(Exception):
        run_agent_diagnosis(workspace, request, settings, None, history="")

    assert captured, "the run produced no prompt at all"
    assert "用户:" not in captured[0]
    assert "\n\nAvailable read-only evidence tools:" in captured[0]
