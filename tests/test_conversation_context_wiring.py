"""Source-level closure: the conversation window has a production caller (#482).

``context_turns()`` was once a rule stated in six documents, implemented in one
place, and called from ``src/`` zero times. The tests that existed proved the
window *picks* the right turns; none of them could notice that no prompt ever
received them, because a test of the store cannot see a missing call site.

So this guard reads the source, following ``tests/test_answer_caller_shapes.py``:
what is being prevented is a future edit, not a current behaviour — the wiring
being deleted while every unit test still passes. Each assertion has been
watched turn red when the thing it pins is taken away.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
RUNTIME = "gateway_runtime.py"
#: The three generation entry points, and the keyword each takes history by.
GENERATION_CALLS = {
    "run_zero_order_answer": "history",
    "run_agent_diagnosis": "history",
    "_try_customer_rag": "history",
    # The promo card is built here, not in `_try_customer_rag`: passing the
    # history one hop down is not the same as passing it to the prompt.
    "promo_prompt": "history",
}
#: The one caller of a generation entry point that must NOT pass history: the
#: device path (`runs`) has no conversation semantics — PRD #410 puts it out of
#: scope — and it must not acquire one by accident through a shared default.
#: Naming it is the point: if the device path ever grows a conversation, that is
#: a decision, and this assertion should be the thing that asks for it.
CONVERSATIONLESS_CALLERS = {"_execute_run"}
#: The module that reads the window. One definition, or the wiring is a copy.
WINDOW_MODULE = "conversation_context"


def _called_name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _tree(filename: str) -> ast.Module:
    return ast.parse((SOURCE_ROOT / filename).read_text(encoding="utf-8"))


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called_name(node.func) == name]


def test_every_generation_path_hands_over_the_history() -> None:
    """Each producer of a user-facing answer passes a rendered window.

    The three paths build their prompts in different modules, so the wiring
    lives at the point where all three are called — if a path stops receiving
    history, only that path regresses silently.
    """
    tree = _tree(RUNTIME)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    missing: list[str] = []
    for name, keyword in GENERATION_CALLS.items():
        for call in _calls(tree, name):
            owner = call
            while owner in parents:
                owner = parents[owner]
                if isinstance(owner, ast.FunctionDef):
                    break
            if getattr(owner, "name", "") in CONVERSATIONLESS_CALLERS:
                continue
            if not any(kw.arg == keyword for kw in call.keywords):
                missing.append(f"{RUNTIME}:{call.lineno} {name}(...) without {keyword}=")
    assert missing == [], "; ".join(missing)


def test_the_runtime_reads_the_window_through_one_place() -> None:
    """One reader, so "which turns count" is not re-decided per call site."""
    source = (SOURCE_ROOT / RUNTIME).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert _calls(tree, "build_history"), "the runtime never renders a conversation window"
    assert f"{WINDOW_MODULE} import" in source, (
        f"the runtime no longer imports {WINDOW_MODULE}: the window is being read somewhere else"
    )


def test_a_missing_window_is_read_as_no_window() -> None:
    """The reader has an explicit empty answer, not an exception path.

    A question must not become unanswerable because its history could not be
    read; the failure is logged and the prompt is built without it.
    """
    tree = _tree(RUNTIME)
    reader = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_conversation_history"
        ),
        None,
    )
    assert reader is not None, "the one reader of the conversation window is gone"
    returns = [node.value for node in ast.walk(reader) if isinstance(node, ast.Return)]
    assert any(isinstance(value, ast.Constant) and value.value == "" for value in returns), (
        "the reader no longer has an empty-history return"
    )
    assert any(isinstance(node, ast.Try) and node.handlers for node in ast.walk(reader)), (
        "an unreadable window would now fail the question instead of yielding no history"
    )
