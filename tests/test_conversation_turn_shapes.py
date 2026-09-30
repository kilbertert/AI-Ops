"""Source-level closure for the conversation-turn contract (#172).

The rule: **a turn claimed for a conversation is either handed to the worker
that will finish it, or released on the spot.** Nothing in between. Two ways to
break it, both of which this repository has already shipped:

- *released at submit* — the diagnosis branches used to free the slot the
  moment the job was queued. The row stayed, the answer never arrived, and
  ``is_generating`` reported ``false`` for the whole generation, so the
  frontend's concurrency gate was blind and the follow-up never saw the turn.
- *claimed and forgotten* — a branch that begins a turn and passes it nowhere
  wedges the conversation at 409 until the claim self-expires.

Neither is visible from one endpoint's behaviour: the defect is the *absence*
of a hand-off in a branch that otherwise answers correctly. So this guard reads
the source, following ``tests/test_answer_caller_shapes.py``: what is being
prevented is a future edit, not a current behaviour. Every assertion here has
been watched turn red when the thing it pins is taken away — a guard nobody has
seen fail is not evidence.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
#: The module that owns every claim site.
API_FILE = "gateway_api.py"
#: The claim. Every branch that wants the conversation's slot goes through it.
CLAIM = "_begin_conversation_turn"
#: The file that may define the claim rather than call it.
CLAIM_DEFINER = API_FILE
#: Helpers that take the claimed turn away again (release / hand-off plumbing).
#: ``_keep_conversation_turn`` is deliberately absent: it was the "persist
#: without an answer" marker the diagnosis line used, and it is what let a
#: claimed turn sit answer-less forever.
RELEASING = "_release_conversation_turn"
#: The runtime entry points that accept a claimed turn. A branch that claims a
#: turn must pass it to exactly one of these, or release it.
HANDOFFS = {"start_assistant_qa": "conversation_turn_no", "start_standard_diagnosis": "conversation_turn"}


def _called_name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone: this guard points at nothing")


def _claim_sites(tree: ast.Module) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _called_name(node.func) == CLAIM
    ]


#: Fields that hold a statement list. ``handlers`` is walked separately: it
#: holds ``ExceptHandler`` nodes, which carry their own ``body``.
BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _block_owners(tree: ast.Module) -> list[tuple[ast.AST | None, list[ast.stmt]]]:
    """Every statement list in ``tree`` paired with the node that owns it."""
    found: list[tuple[ast.AST | None, list[ast.stmt]]] = [(None, tree.body)]
    for node in ast.walk(tree):
        for field in BLOCK_FIELDS:
            block = getattr(node, field, None)
            if isinstance(block, list) and block:
                found.append((node, block))
        for handler in getattr(node, "handlers", []) or []:
            if handler.body:
                found.append((handler, handler.body))
    return found


def _innermost_block(tree: ast.Module, target: ast.AST) -> tuple[ast.AST | None, list[ast.stmt]]:
    """The block whose statement directly contains ``target``.

    Several blocks contain it transitively; the innermost one is the deepest
    compound statement, which is the one with the greatest start line.
    """
    matches = [
        (owner, block)
        for owner, block in _block_owners(tree)
        if any(any(node is target for node in ast.walk(statement)) for statement in block)
    ]
    if not matches:
        raise AssertionError(f"line {getattr(target, 'lineno', '?')} sits in no block")
    return max(matches, key=lambda item: getattr(item[0], "lineno", 0))


def _holds(block: list[ast.stmt], target: ast.AST) -> ast.stmt:
    return next(statement for statement in block if any(node is target for node in ast.walk(statement)))


def _scope_of(tree: ast.Module, claim: ast.Call) -> tuple[ast.stmt, list[ast.stmt]]:
    """What a claim is scoped to: its statement and the rest of its block.

    A claim sits in a *block*, not in a function: the work it belongs to is what
    follows it there. Scoping to the enclosing function instead lets a sibling
    branch satisfy the guard -- and both halves of this defect lived in one
    ``if`` inside a 300-line handler holding four claims.

    A block whose only statement is the claim (``if conversation is not None:
    turn_no = claim(...)``) is a guard, not the work: the hand-off follows in
    the *enclosing* block, so the walk steps outward past it first.
    """
    owner, block = _innermost_block(tree, claim)
    while len(block) == 1 and owner is not None:
        owner, block = _innermost_block(tree, owner)
    statement = _holds(block, claim)
    return statement, block[block.index(statement) :]


def _calls_in(block: list[ast.stmt]) -> list[ast.Call]:
    return [node for statement in block for node in ast.walk(statement) if isinstance(node, ast.Call)]


def _handoffs(scope: list[ast.Call]) -> set[str]:
    """Runtime entry points the block hands a turn to, by keyword name."""
    found: set[str] = set()
    for node in scope:
        name = _called_name(node.func)
        if name in HANDOFFS and any(kw.arg == HANDOFFS[name] for kw in node.keywords):
            found.add(name)
    return found


def _releases(scope: list[ast.Call]) -> int:
    return sum(1 for node in scope if _called_name(node.func) == RELEASING)


def test_the_helper_that_persisted_a_turn_without_an_answer_is_gone() -> None:
    """``_keep_conversation_turn`` is the defect's shape, not a helper.

    It wrote the row answer-less and freed the slot at submit time. Anything
    that reintroduces that shape reintroduces both halves of the bug at once.
    """
    tree = ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))
    offenders = [
        f"{API_FILE}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_keep_conversation_turn"
    ]
    assert offenders == [], f"a claimed turn is being persisted without an answer again: {offenders}"


def test_every_claim_site_hands_its_turn_to_a_worker() -> None:
    """Each branch that claims the slot must give the turn to a job worker.

    The claim exists so the worker can fill it in at the terminal state. A
    branch that claims and hands the turn nowhere holds the conversation busy
    for the length of the self-expiring lock and leaves no answer behind.
    """
    tree = ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))
    offenders = []
    for call in _claim_sites(tree):
        statement, block = _scope_of(tree, call)
        if not _handoffs(_calls_in(block)):
            offenders.append(
                f"{API_FILE}:{statement.lineno} the block claiming a turn at line {call.lineno}"
                f" hands it nowhere (block is {len(block)} statements)"
            )
    assert offenders == [], "; ".join(offenders)


def test_the_handoff_parameter_is_wired_through_the_runtime() -> None:
    """The claimed turn must reach the code that writes the answer back.

    A branch can hand a turn to the runtime while the runtime drops it on the
    floor; the endpoint still answers 202 and the row still never gets an
    answer. Both hops are pinned here, in the runtime that owns the writes.
    """
    tree = ast.parse((SOURCE_ROOT / "gateway_runtime.py").read_text(encoding="utf-8"))
    for entry, parameter in HANDOFFS.items():
        signature = _function(tree, entry)
        names = {arg.arg for arg in signature.args.args + signature.args.kwonlyargs}
        assert parameter in names, f"{entry}() no longer accepts {parameter}: the turn cannot reach it"
    _function(tree, "_complete_conversation_turn")  # raises if the one shared write is gone


def test_a_release_path_survives_on_every_claiming_branch() -> None:
    """The slot must be freeable when the job never starts.

    Every claiming branch answers 503 when the runtime refuses the job; if that
    path cannot release, the conversation stays busy until the claim expires.
    """
    tree = ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))
    offenders = []
    for call in _claim_sites(tree):
        statement, block = _scope_of(tree, call)
        if _releases(_calls_in(block)) == 0:
            offenders.append(
                f"{API_FILE}:{statement.lineno} the block claiming a turn at line {call.lineno}"
                " has no release path"
            )
    assert offenders == [], "; ".join(offenders)
