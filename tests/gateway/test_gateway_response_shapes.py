"""One 202 shape, one set of status words (#488).

Two defects with the same root cause, and the same reason no behaviour test
could see either:

* `_assistant_question_response` calls itself "the one public shape of an
  assistant-question job" — and two branches built that body inline instead.
  Every field matched, so every test passed; the copy was invisible until
  someone changed one of them.
* `_health_job_response` wrote `{"queued", "running"}` as a literal while the
  store already exported `ACTIVE_HEALTH_JOB_STATUSES` with exactly that value.
  Adding a status to the store would leave this surface reporting
  `retry_after_ms=None` for a job that is still running.

Both are "the same thing defined twice, each copy covered by its own endpoints'
tests". A reading of the response cannot detect a second copy that happens to
agree today, so both halves are asserted at the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
API_FILE = "gateway/gateway_api.py"
#: The module that owns the assistant-question body.
SHAPE_HELPER = "_assistant_question_response"
#: A 202 body that is written out by hand instead of through the helper.
INLINE_QA_SHAPE_MARKER = '"type": "qa"'
#: Status literals that belong to the store's own sets, not to a response body.
OWNED_BY_THE_STORE = {"queued", "running"}


def _api_paths() -> list[Path]:
    routers = sorted((SOURCE_ROOT / "gateway" / "routers").glob("*.py"))
    return [SOURCE_ROOT / API_FILE, *routers]


def _api_tree() -> ast.Module:
    return ast.parse((SOURCE_ROOT / API_FILE).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone: this guard points at nothing")


def _dict_literals(function: ast.AST) -> list[ast.Dict]:
    return [node for node in ast.walk(function) if isinstance(node, ast.Dict)]


def _has_qa_type_entry(body: ast.Dict) -> bool:
    """用 AST 键值常量判定字典字面量是否包含 `type="qa"`，不依赖引号样式。"""
    return any(
        isinstance(key, ast.Constant)
        and key.value == "type"
        and isinstance(value, ast.Constant)
        and value.value == "qa"
        for key, value in zip(body.keys, body.values, strict=True)
    )


def test_the_202_body_is_built_through_the_one_helper() -> None:
    """No handler writes an assistant-question 202 body by hand.

    Counted, not "is the helper called somewhere": the helper IS called — the
    two inline branches sit beside callers that go through it, which is exactly
    how a copy survives review. Scans both `gateway_api.py` and `gateway/routers/*.py`.
    """
    offenders: list[str] = []
    for path in _api_paths():
        rel = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in {SHAPE_HELPER, "create_gateway_app", "register"}:
                continue
            for body in _dict_literals(node):
                keys = {key.value for key in body.keys if isinstance(key, ast.Constant)}
                if _has_qa_type_entry(body) and "qa_id" in keys:
                    offenders.append(f"{rel}:{body.lineno} {node.name} hand-builds a qa body")
    assert offenders == [], "; ".join(offenders)


def test_the_helper_is_the_only_place_that_names_the_qa_kind() -> None:
    """`"type": "qa"` is written once, in the helper.

    The string is the wire contract's discriminator; a second literal is how a
    branch starts growing its own idea of what the body is.
    """
    count = sum(
        1
        for path in _api_paths()
        for body in _dict_literals(ast.parse(path.read_text(encoding="utf-8")))
        if _has_qa_type_entry(body)
    )
    assert count == 1, (
        f"{INLINE_QA_SHAPE_MARKER} appears {count} times across gateway API and routers;"
        " the qa body is being written out by hand again"
    )


def test_the_status_words_come_from_the_store() -> None:
    """`retry_after_ms` asks the store's sets, never a local literal.

    A literal that agrees with the store today is the shape this ticket is
    about: it drifts the moment the store gains a status, and the response then
    lies about whether the client should keep polling.
    """
    offenders: list[str] = []
    for path in _api_paths():
        rel = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in {"create_gateway_app", "register"}:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Set):
                    continue
                words = {element.value for element in inner.elts if isinstance(element, ast.Constant)}
                if words and words <= OWNED_BY_THE_STORE and len(words) > 1:
                    offenders.append(f"{rel}:{inner.lineno} {node.name} writes the status set out by hand")
    assert offenders == [], "; ".join(offenders)
    # And the response really does ask the store, so deleting the import instead
    # of the literal is not a way to satisfy the check above.
    tree = _api_tree()
    helper = _function(tree, "_health_job_response")
    called = {getattr(inner.func, "id", "") for inner in ast.walk(helper) if isinstance(inner, ast.Call)} | {
        getattr(inner, "id", "") for inner in ast.walk(helper) if isinstance(inner, ast.Name)
    }
    assert "ACTIVE_HEALTH_JOB_STATUSES" in called, (
        "_health_job_response no longer asks the store whether a job is active"
    )
