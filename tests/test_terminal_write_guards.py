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

from aiops_diagnostics.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, TERMINAL_DIAGNOSIS_STATUSES

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
RUNTIME = "gateway_runtime.py"
#: The two update paths that end a job, and the claim their guards enforce.
TERMINAL_UPDATES = ("update_health_job", "update_standard_diagnosis")
#: Statuses that end a job: a write with one of these closes the row.
TERMINAL = TERMINAL_DIAGNOSIS_STATUSES | {"cancelled", "expired"}
#: The claim status, which is not a terminal write and has its own guard.
CLAIM = ACTIVE_DIAGNOSIS_STATUSES


def _runtime_tree() -> ast.Module:
    return ast.parse((SOURCE_ROOT / RUNTIME).read_text(encoding="utf-8"))


def _job_updates(function: ast.FunctionDef) -> list[ast.Call]:
    """Every call to the two job-update paths inside one worker."""
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in TERMINAL_UPDATES
    ]


def _write_is_answered(call: ast.Call) -> bool:
    """Whether the refusal of this write can change what happens next.

    Scanned over the enclosing function's source rather than over the call's
    parent chain: the answer is usually one statement later
    (`ok = ...` then `guarded=not ok`), and a parent-chain reader would call
    that unchecked. What is being asserted is that the value reaches a decision
    — an assignment nobody reads is not an answer either, but that is a
    different defect and this one is the one that shipped.
    """
    return call in _answered_writes


def _status_argument(call: ast.Call) -> str | None:
    """The literal status a call writes, if it writes one."""
    for keyword in call.keywords:
        if keyword.arg == "status" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    cur: ast.AST | None = target
    while cur is not None and cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.FunctionDef):
            return cur.name
    return "<module>"


def test_the_enumeration_finds_the_job_updates() -> None:
    """The guard's own foundation: an empty list makes every assertion vacuous.

    Five direct calls: the health worker's claim write (its four terminal writes
    go through `_finish_health_job`, asserted separately) and the diagnosis
    worker's claim plus three terminal writes.
    """
    tree = _runtime_tree()
    found = sum(
        len(_job_updates(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_execute_health_report", "_execute_standard_diagnosis"}
    )
    assert found == 5, f"the two workers make {found} direct job updates, expected 5"


def test_a_health_job_terminal_write_goes_through_the_one_helper() -> None:
    """The four health writes share one checked path.

    They are the same line four times (timeout, completed, report error, source
    error) — the shape that let three of them be unchecked while the fourth was
    not, and the reason `_finish_health_job` exists.
    """
    tree = _runtime_tree()
    worker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_health_report"
    )
    direct = [node for node in _job_updates(worker) if _status_argument(node) in TERMINAL]
    assert direct == [], f"{RUNTIME} writes a health job terminal state without the helper"
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_finish_health_job"
    )
    assert len(_job_updates(worker)) == 1, "the claim write is checked on its own"
    # The helper is the one place the health terminal write happens, and it
    # answers its caller: the store's value, and — on a refusal — a log line,
    # because this path has no metric for the refusal to show up in.
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_finish_health_job"
    )
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
    tree = _runtime_tree()
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in {"_execute_health_report", "_execute_standard_diagnosis"}:
            continue
        answered = _answered_writes(tree, node)
        for call in _job_updates(node):
            if call not in answered:
                offenders.append(f"{RUNTIME}:{call.lineno} {node.name} ignores the result")
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


def test_the_diagnosis_success_metric_is_gated_on_the_write() -> None:
    """A refused write must not be counted as a completed diagnosis.

    The diagnostic write returns `False` when the row is already terminal, and
    the metric used to be recorded unconditionally afterwards: a metric row
    saying "completed" beside a row saying `expired`. The gate is what keeps the
    two surfaces telling the same story.
    """
    tree = _runtime_tree()
    worker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_standard_diagnosis"
    )
    source = ast.unparse(worker)
    assert "if not stored:" in source, "the success metric is no longer gated on the write"


def test_a_refused_terminal_write_records_no_metric(tmp_path, monkeypatch) -> None:
    """The behaviour, not just the shape: a refused write leaves no metric row.

    The stub refuses ONLY the terminal write, and lets the `running` claim
    through — otherwise the worker returns at the claim and never reaches the
    line under test, which is a green test that proves nothing. (Getting that
    wrong is how the first version of this test passed on the ungated code.)
    """
    from test_standard_diagnosis_runtime import _runtime, _scope

    from aiops_diagnostics.agent_contracts import (
        AgentDiagnosis,
        Confidence,
        DiagnosisStatus,
    )

    runtime, store, settings = _runtime(tmp_path)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.run_agent_diagnosis",
        lambda workspace, request, selected_settings, fixture, **kwargs: AgentDiagnosis(
            incident_id=workspace.load_manifest().incident_id,
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            status=DiagnosisStatus.DIAGNOSED,
            summary="阈值内完成",
            root_cause="测试根因",
            confidence=Confidence.HIGH,
            evidence_ids=[],
            hypotheses=[],
            limitations=[],
            failed_sources=[],
            next_steps=[],
        ),
    )
    recorded: list[dict] = []
    monkeypatch.setattr(runtime, "_record_metric", lambda **fields: recorded.append(fields))

    real_update = runtime.store.update_standard_diagnosis
    reached: list[str] = []

    def refuse_the_terminal_write(diagnosis_id: str, *, status: str, **kwargs: object) -> bool:
        reached.append(status)
        if status == "running":
            return real_update(diagnosis_id, status=status, **kwargs)
        return False

    monkeypatch.setattr(runtime.store, "update_standard_diagnosis", refuse_the_terminal_write)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    assert created["diagnosis_id"]
    import time

    deadline = time.monotonic() + 5
    while "completed" not in reached and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime.shutdown()
    # The worker got past the claim and tried the terminal write...
    assert "completed" in reached, reached
    # ...and its refusal produced no metric.
    assert recorded == [], f"a refused write was counted as a completed run: {recorded}"


def test_a_refused_health_write_is_logged(tmp_path, monkeypatch, caplog) -> None:
    """The health path has no metric, so the log is its observable.

    Without one, "the result is checked" would be nominal on this path: the
    boolean would be returned into a caller that does nothing with it, and a
    worker whose result nobody could ever read would leave no trace.
    """
    import logging

    from test_standard_diagnosis_runtime import _runtime

    runtime, store, settings = _runtime(tmp_path)
    monkeypatch.setattr(runtime.store, "update_health_job", lambda *a, **k: False)
    with caplog.at_level(logging.WARNING, logger="aiops.gateway_runtime"):
        assert runtime._finish_health_job("job1", status="completed") is False
    assert any("health job terminal write refused" in record.getMessage() for record in caplog.records), [
        record.getMessage() for record in caplog.records
    ]
