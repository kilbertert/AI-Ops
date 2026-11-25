"""Terminal writes answer to the row, everywhere (#493).

A job's terminal write can be refused: the row expired, or another path drove it
terminal first. Every line that writes one has to notice, and the three
asynchronous tables used to disagree about whether it did:

* `assistant_questions` checked all nine of its terminal writes (#355) — refused
  means quiet exit, no metric, no conversation turn;
* `_execute_standard_diagnosis` checked its `running` claim but not its three
  terminal writes, and then **unconditionally** recorded a metric;
* `_execute_health_report` checked its `running` claim and none of its four
  terminal writes (it records no metrics at all).

So a refused write left the caller polling a row reading `expired` while the
metrics said the job completed. Two surfaces, one run, two answers.

Two kinds of assertion, because the defect has two shapes:

* **behavioural** — a refused terminal write produces no metric row;
* **structural** — every terminal write in the package is checked, so the next
  worker cannot be written the unchecked way. Checked at the source, because a
  *missing* check is invisible to any test that drives a successful run.
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiops_diagnostics.gateway.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, TERMINAL_DIAGNOSIS_STATUSES

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
RUNTIME = "gateway/gateway_runtime.py"
CHAT_RUNNER = "gateway/chat_job_runner.py"
TRIAL_RUNNER = "gateway/trial_job_runner.py"
HEALTH_RUNNER = "health/health_job_runner.py"
OPS_RUNNER = "operator/ops_analysis_runner.py"
WORKER_MODULES = (RUNTIME, CHAT_RUNNER, TRIAL_RUNNER, HEALTH_RUNNER, OPS_RUNNER)
#: The two update paths that end a job, and the claim their guards enforce.
TERMINAL_UPDATES = ("update_health_job", "update_standard_diagnosis")
#: Statuses that end a job: a write with one of these closes the row.
TERMINAL = TERMINAL_DIAGNOSIS_STATUSES | {"cancelled", "expired"}
#: The claim status, which is not a terminal write and has its own guard.
CLAIM = ACTIVE_DIAGNOSIS_STATUSES


def _runtime_tree() -> ast.Module:
    return ast.parse((SOURCE_ROOT / RUNTIME).read_text(encoding="utf-8"))


def _worker_trees() -> list[tuple[str, ast.Module]]:
    return [(rel, ast.parse((SOURCE_ROOT / rel).read_text(encoding="utf-8"))) for rel in WORKER_MODULES]


def _job_updates(function: ast.FunctionDef) -> list[ast.Call]:
    """Every call to the two job-update paths inside one worker."""
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in TERMINAL_UPDATES
    ]


def _status_argument(call: ast.Call) -> str | None:
    """The literal status a call writes, if it writes one."""
    for keyword in call.keywords:
        if keyword.arg == "status" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def test_the_enumeration_finds_the_job_updates() -> None:
    """The guard's own foundation: an empty list makes every assertion vacuous.

    Health worker's claim write (its four terminal writes go through
    `finish_health_job`, asserted separately).
    """
    found = sum(
        len(_job_updates(node))
        for _, tree in _worker_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"execute_health_report", "_execute_health_report", "_execute_standard_diagnosis"}
    )
    assert found == 1, f"the health worker makes {found} direct job updates, expected 1"


def test_a_health_job_terminal_write_goes_through_the_one_helper() -> None:
    """The four health writes share one checked path.

    They are the same line four times (timeout, completed, report error, source
    error) — the shape that let three of them be unchecked while the fourth was
    not, and the reason `finish_health_job` exists.
    """
    tree = ast.parse((SOURCE_ROOT / HEALTH_RUNNER).read_text(encoding="utf-8"))
    worker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "execute_health_report"
    )
    direct = [node for node in _job_updates(worker) if _status_argument(node) in TERMINAL]
    assert direct == [], f"{HEALTH_RUNNER} writes a health job terminal state without the helper"
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "finish_health_job"
    )
    assert len(_job_updates(worker)) == 1, "the claim write is checked on its own"
    source = ast.unparse(helper)
    assert "update_health_job" in source, "the helper no longer writes the job"
    assert "return True" in source and "return False" in source, source
    assert "_LOGGER.warning" in source, "a refusal on this path leaves no trace"


def test_every_job_write_in_the_two_workers_is_answered() -> None:
    """No worker writes a job state and ignores whether it landed.

    The defect was a *missing* check, which no test that drives a successful run
    can see: the answer is right either way, and only the refusal differs. So
    the rule is asserted where it lives — on the write's neighbourhood — and it
    covers the claim writes too, because those had the check while the terminal
    writes beside them did not.
    """
    offenders: list[str] = []
    for rel, tree in _worker_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in {
                "execute_health_report",
                "_execute_health_report",
                "_execute_standard_diagnosis",
                "execute_trial",
            }:
                continue
            answered = _answered_writes(tree, node)
            for call in _job_updates(node):
                if call not in answered:
                    offenders.append(f"{rel}:{call.lineno} {node.name} ignores the result")
    assert offenders == [], "; ".join(offenders)


def _answered_writes(tree: ast.Module, worker: ast.FunctionDef) -> set[ast.Call]:
    """Updates whose return value reaches a decision, per worker.

    Two spellings count, and they are the two the repository actually uses:
    the call sitting inside an `if not ...:` test, and the call assigned to a
    name that appears again in the same function (`ok = ...` then
    `guarded=not ok`). A bare statement call appears in neither.
    """
    answered: set[ast.Call] = set()
    for call in _job_updates(worker):
        for node in ast.walk(worker):
            if isinstance(node, ast.If) and any(inner is call for inner in ast.walk(node.test)):
                answered.add(call)
        # Assignment: the value is answered if the name it bound is read again.
        for node in ast.walk(worker):
            if not isinstance(node, ast.Assign) or not any(inner is call for inner in ast.walk(node.value)):
                continue
            for target in node.targets:
                name = getattr(target, "id", "")
                if not name:
                    continue
                uses = [
                    inner
                    for inner in ast.walk(worker)
                    if isinstance(inner, ast.Name) and inner.id == name and inner is not target
                ]
                if uses:
                    answered.add(call)
    return answered


def test_a_refused_health_write_is_logged(tmp_path, monkeypatch, caplog) -> None:
    """The health path has no metric, so the log is its observable.

    Without one, "the result is checked" would be nominal on this path: the
    boolean would be returned into a caller that does nothing with it, and a
    worker whose result nobody could ever read would leave no trace.
    """
    import logging

    from operations.test_ops_analysis_runtime import _runtime

    runtime, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime.store, "update_health_job", lambda *a, **k: False)
    with caplog.at_level(logging.WARNING, logger="aiops.gateway_runtime"):
        assert runtime._finish_health_job("job1", status="completed") is False
    assert any("health job terminal write refused" in record.getMessage() for record in caplog.records), [
        record.getMessage() for record in caplog.records
    ]


def _submitted_workers(trees: list[tuple[str, ast.Module]]) -> set[str]:
    """Every worker this runtime hands to the thread pool, discovered from the code.

    **Discovered, not listed.** A fixed list is the wrong shape for this pin: a
    sixth worker added later would simply not appear in it, and the check would
    keep passing while covering five of six — the same "guard that finds no call
    site passes vacuously" failure this file exists to catch (raised in the
    #730 review). Reading every `_executor.submit(...)` call site across
    `gateway_runtime.py` and its delegated runners (including local variables
    bound to `*_execute_*` within the same function) makes a new worker enter
    the check by construction.
    """
    found: set[str] = set()
    for _, tree in trees:
        for func_def in ast.walk(tree):
            if not isinstance(func_def, ast.FunctionDef):
                continue
            bound_workers: dict[str, set[str]] = {}
            for stmt in ast.walk(func_def):
                if isinstance(stmt, ast.Assign):
                    targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                    worker_names = {
                        inner.attr
                        for inner in ast.walk(stmt.value)
                        if isinstance(inner, ast.Attribute) and inner.attr.startswith("_execute_")
                    }
                    if worker_names:
                        for target in targets:
                            bound_workers.setdefault(target, set()).update(worker_names)
            for node in ast.walk(func_def):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "submit"):
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Attribute) and arg.attr.startswith("_execute_"):
                        found.add(arg.attr)
                    elif isinstance(arg, ast.Name) and arg.id in bound_workers:
                        found.update(bound_workers[arg.id])
    return found


def test_every_async_worker_converges_on_any_exception() -> None:
    """A worker that catches an enumerated list of classes does not converge.

    The rule is not "the right classes are listed" — it is that **nothing a
    worker can raise may escape it**, because escaping means no terminal row:
    the job sits on `running` until its deadline and the caller cannot tell it
    apart from work still in progress.

    That is not hypothetical. It was reached by a different exception class
    three separate times, each fixed by appending one name:

    * `AgentRuntimeError` (a `RuntimeError`) — #700's first end-to-end run;
    * `TypeError` from a call site that had lost an argument — #700's review;
    * `ScopeError` (also a `RuntimeError`, raised by `resolve_query_scope`
      **inside the very `try`**) — #721's acceptance run, in the two analysis
      workers, and still present in the other three until #725.

    Enumerating cannot converge: every pass adds one class and leaves the next
    outside. So the pin is on the *shape* — each worker's handler must catch
    `Exception` — and the assertion is written over all five workers at once so
    that a sixth cannot be added the old way.
    """
    worker_trees = _worker_trees()
    tree = worker_trees[0][1]
    workers = _submitted_workers(worker_trees)
    assert workers, "found no submitted workers — the discovery rule is broken, not the code"
    runner_defs: dict[str, ast.FunctionDef] = {}
    for _, mod_tree in worker_trees[1:]:
        for node in ast.walk(mod_tree):
            if isinstance(node, ast.FunctionDef):
                runner_defs[node.name] = node
    seen: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in workers:
            continue
        target = node
        handlers = [
            handler
            for try_node in ast.walk(target)
            if isinstance(try_node, ast.Try)
            for handler in try_node.handlers
        ]
        if not handlers:
            # 委托给领域执行模块时，核对实际执行体而非薄壳本身。
            delegated = next(
                (
                    runner_defs[call.func.attr]
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in runner_defs
                ),
                None,
            )
            if delegated is not None:
                handlers = [
                    handler
                    for try_node in ast.walk(delegated)
                    if isinstance(try_node, ast.Try)
                    for handler in try_node.handlers
                ]
        seen[node.name] = [
            ast.unparse(handler.type) if handler.type is not None else "bare except" for handler in handlers
        ]

    # Every discovered worker must be a real function in this module: discovery
    # that matches a name with no definition would silently cover nothing.
    assert set(seen) == workers, f"submitted but not defined here: {sorted(workers - set(seen))}"
    assert {"_execute_chat_dify_qa", "_execute_chat_order_qa", "_execute_trial"} <= set(seen)
    offenders = {
        name: caught
        for name, caught in seen.items()
        if not any(spelling in {"Exception", "BaseException"} for spelling in caught)
    }
    assert offenders == {}, (
        "an escaping exception means no terminal row, and a list of classes always "
        f"leaves the next one outside; workers without `except Exception`: {offenders}"
    )


def test_chat_workers_answer_every_assistant_question_write() -> None:
    """聊天异步执行体对 `assistant_questions` 的认领与成功终态写必须检查返回值。"""
    tree = ast.parse((SOURCE_ROOT / CHAT_RUNNER).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in {
            "execute_chat_dify_qa",
            "execute_chat_order_qa",
        }:
            continue
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "update_assistant_question"
        ]
        for call in calls:
            status = _status_argument(call)
            if status not in {"running", "completed"}:
                continue
            checked = any(
                isinstance(if_node, ast.If) and any(inner is call for inner in ast.walk(if_node.test))
                for if_node in ast.walk(node)
            )
            if not checked:
                offenders.append(f"{CHAT_RUNNER}:{call.lineno} {node.name} ignores status={status!r}")
    assert offenders == [], "; ".join(offenders)
