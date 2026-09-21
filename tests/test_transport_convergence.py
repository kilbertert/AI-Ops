"""Guard tests for #312: the transport skeleton stays the only way out.

Issue #288 converged eleven hand-rolled HTTP call sites onto one skeleton. The
convergence is only durable if a new integration cannot quietly go back to
hand-rolling its own request, or its own backoff. These are source-level
assertions rather than behaviour tests, because the thing being prevented is a
future edit, not a current behaviour.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
SKELETON = "bounded_http.py"


def _python_sources() -> list[Path]:
    return sorted(path for path in SOURCE_ROOT.glob("*.py") if path.name != SKELETON)


def test_only_the_skeleton_constructs_a_urllib_request() -> None:
    """Every outbound HTTP request must be assembled by the skeleton.

    A new client that builds its own ``urllib.request.Request`` gets its own
    header assembly, its own exception capture set and its own status-code
    table -- which is exactly the drift #288 existed to remove.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "Request":
                continue
            chain = []
            cursor: ast.expr = node
            while isinstance(cursor, ast.Attribute):
                chain.append(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                chain.append(cursor.id)
            if list(reversed(chain)) == ["urllib", "request", "Request"]:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"urllib.request.Request must be constructed only in {SKELETON}; found: {', '.join(offenders)}"
    )


def test_only_the_skeleton_computes_a_retry_delay() -> None:
    """One backoff implementation: the skeleton's jittered, capped one.

    A second implementation is how the video media retry came to differ from
    the gateway client's backoff -- same intent, different numbers, nobody
    able to say which was correct. A caller that needs different numbers passes
    a ``RetryPolicy``; it does not compute its own sleeps.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # `time.sleep(...)` parses as an Attribute, a bare `sleep(...)` as a
            # Name; both are the thing being guarded against.
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called != "sleep":
                continue
            # Any power operator anywhere in the sleep argument is an
            # exponential backoff by hand -- nesting like `0.25 * (2**n)` means
            # a shallow scan of the top-level op alone would miss it.
            if any(isinstance(inner, ast.Pow) for arg in node.args for inner in ast.walk(arg)):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "exponential backoff must come from the skeleton's retry policy; "
        f"found a hand-rolled `sleep(<power>)` at {', '.join(offenders)}"
    )
