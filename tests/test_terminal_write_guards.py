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
import threading
import time
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
    finished = threading.Event()

    def refuse_the_terminal_write(diagnosis_id: str, *, status: str, **kwargs: object) -> bool:
        if status == "running":
            return real_update(diagnosis_id, status=status, **kwargs)
        finished.set()
        return False

    monkeypatch.setattr(runtime.store, "update_standard_diagnosis", refuse_the_terminal_write)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    assert created["diagnosis_id"]
    # Wait for the WORKER to finish, not for a flag it set on its way in: the
    # metric is recorded after the write returns, so an assertion that fires
    # the moment the write is called reads a state the worker has not reached.
    assert finished.wait(10), "the worker never attempted its terminal write"
    # The metric (if the gate were missing) is recorded right after that write,
    # so give the worker the moment it needs to get there.
    runtime.shutdown()
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


def _submitted_workers(tree: ast.Module) -> set[str]:
    """Every worker this runtime hands to the thread pool, discovered from the code.

    **Discovered, not listed.** A fixed list is the wrong shape for this pin: a
    sixth worker added later would simply not appear in it, and the check would
    keep passing while covering five of six — the same "guard that finds no call
    site passes vacuously" failure this file exists to catch (raised in the
    #730 review). Reading the `self._executor.submit(...)` call sites makes a new
    worker enter the check by construction.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "submit"):
            continue
        for arg in node.args:
            name = getattr(arg, "attr", None)
            if isinstance(arg, ast.Attribute) and name and name.startswith("_execute_"):
                found.add(name)
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
    tree = _runtime_tree()
    workers = _submitted_workers(tree)
    assert workers, "found no submitted workers — the discovery rule is broken, not the code"
    seen: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in workers:
            continue
        handlers = [
            handler
            for try_node in ast.walk(node)
            if isinstance(try_node, ast.Try)
            for handler in try_node.handlers
        ]
        seen[node.name] = [
            ast.unparse(handler.type) if handler.type is not None else "bare except" for handler in handlers
        ]

    # Every discovered worker must be a real function in this module: discovery
    # that matches a name with no definition would silently cover nothing.
    assert set(seen) == workers, f"submitted but not defined here: {sorted(workers - set(seen))}"
    offenders = {
        name: caught
        for name, caught in seen.items()
        if not any(spelling in {"Exception", "BaseException"} for spelling in caught)
    }
    assert offenders == {}, (
        "an escaping exception means no terminal row, and a list of classes always "
        f"leaves the next one outside; workers without `except Exception`: {offenders}"
    )


def _runtime_with_settings(tmp_path: Path, monkeypatch):
    """A runtime whose `Settings.from_config` the test can make fail.

    The config file itself is not what matters — what the worker does when loading
    it raises is.
    """
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore

    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    runtime_settings = Settings()
    runtime_settings.agent.run_root = str(tmp_path / "runs")
    return GatewayRuntime(GatewayStore(gateway_settings.database_file), gateway_settings, runtime_settings)


def test_an_unreadable_config_still_ends_the_job(tmp_path, monkeypatch) -> None:
    """A **supported** failure of `Settings.from_config` must not strand a job (#730).

    Three workers load settings after claiming the row `running`, and
    `Settings.from_config` raises `ValueError` when the configured file is missing
    or invalid — a config file removed after startup is an ordinary operational
    event, not a programming error. Two of the three loaded it **before** the block
    that catches anything, so the row stayed `running` until its deadline: the same
    "caller cannot tell it apart from work still in progress" shape as #700/#721,
    reached through a gap between the claim and the `try` rather than a class the
    tuple omitted.

    The rule the #730 review states: **everything after the `running` claim belongs
    inside the catchable block.** This drives the diagnosis worker, which had the
    widest gap, and asserts the row terminates.
    """
    from test_standard_diagnosis_runtime import _scope

    runtime = _runtime_with_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.Settings.from_config",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("server config does not exist")),
    )
    try:
        created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in TERMINAL_DIAGNOSIS_STATUSES:
            assert time.monotonic() < deadline, current
            time.sleep(0.01)
            current = runtime.store.get_standard_diagnosis(
                created["diagnosis_id"], _scope().scope_fingerprint
            )
    finally:
        runtime.shutdown()

    assert current["status"] == "failed", current
    assert current["error_code"] == "DIAGNOSIS_FAILED", current


def test_a_newly_caught_exception_keeps_its_text_off_the_wire(tmp_path, monkeypatch) -> None:
    """The health surface RETURNS `error_message` verbatim — so it must carry no internals.

    `_health_job_response` hands the stored `error_message` to the caller as
    `error.message`. `_internal_error_message` **redacts**: it strips PII and leaves
    internal diagnostics in place — right for a record an engineer reads, wrong for a
    response body. So a newly-caught class contributes its **class name only**, while
    the two classes that were already caught keep the shape they already had.

    Security finding in the #730 review.
    """
    from test_standard_diagnosis_runtime import _scope

    from aiops_diagnostics.gateway_api import _health_job_response

    runtime = _runtime_with_settings(tmp_path, monkeypatch)
    secret = "connect to 10.1.2.3:3306 as readonly failed: upstream SDK detail"
    context = _scope()
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.resolve_query_scope",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyError(secret)),
    )
    try:
        job, _ = runtime.store.create_or_reuse_health_job(context.scope_fingerprint, "ORDER-1", "health-v1")
        runtime._execute_health_report(job["job_id"], context, "ORDER-1")
        stored = runtime.store.get_health_job(job["job_id"], context.scope_fingerprint)
    finally:
        runtime.shutdown()

    assert stored is not None, "the job row vanished"
    assert stored["error_code"] == "SOURCE_UNAVAILABLE", stored
    assert secret not in str(stored["error_message"]), stored["error_message"]
    # And the wire is what actually matters on this surface.
    assert secret not in _health_job_response(stored)["error"]["message"]
