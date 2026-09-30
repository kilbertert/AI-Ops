"""Routing decisions are counted on both sides (#464).

`classify_with_jev` recorded **only failures**. Zero failure rows are equally
consistent with "Jev decided every question for the last week" and with
"nothing ever asked it" — so #405's observation-window criterion ("Jev's path
ran continuously for ≥ 7 days") could not be evaluated at all. The window still
guards against a regression; what it could not do was produce a positive
statement.

The denominator is the point: a success rate needs both halves, and the two
halves have to share a dimension or the ratio is meaningless.
"""

from __future__ import annotations

from pathlib import Path

from aiops_diagnostics.jev_decisions import JevUnavailable
from aiops_diagnostics.metrics_store import MetricsStore
from aiops_diagnostics.routing import (
    ROUTING_DECIDED,
    ROUTING_UNAVAILABLE,
    RoutingThresholds,
    classify_with_jev,
)


class _Client:
    """A Jev client that either answers or fails, decided per test."""

    def __init__(self, *, decision: dict | None = None, fails: Exception | None = None) -> None:
        self.decision = decision
        self.fails = fails
        self.calls = 0

    def decide(self, question: str, thresholds: RoutingThresholds):
        del question, thresholds
        self.calls += 1
        if self.fails is not None:
            raise self.fails
        return self.decision


class _Decision:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def as_dict(self) -> dict:
        return self.payload


def _store(tmp_path: Path) -> MetricsStore:
    return MetricsStore(tmp_path / "metrics.db")


def test_a_decision_is_counted(tmp_path: Path) -> None:
    """The half that was missing.

    Without this row, "zero failures in the window" is an observation that
    cannot distinguish a working week from an idle one.
    """
    store = _store(tmp_path)
    client = _Client(decision=_Decision({"risk": "low", "order_no": None}))
    import aiops_diagnostics.routing as routing

    original = routing.decide
    routing.decide = lambda question, client, *, thresholds: client.decide(question, thresholds)
    try:
        result = classify_with_jev(
            "问题", client, thresholds=RoutingThresholds(), metrics=store, tenant_id="T-1"
        )
    finally:
        routing.decide = original

    assert result == {"risk": "low", "order_no": None}
    counts = _counts(store, "T-1")
    assert counts.get(("routing", "completed")) == 1, counts


def test_success_and_failure_share_one_dimension(tmp_path: Path) -> None:
    """A rate needs a common denominator, so both rows carry `routing`."""
    store = _store(tmp_path)
    import aiops_diagnostics.routing as routing

    original = routing.decide
    routing.decide = lambda question, client, *, thresholds: client.decide(question, thresholds)
    try:
        classify_with_jev(
            "问题",
            _Client(decision=_Decision({"risk": "high", "order_no": None})),
            thresholds=RoutingThresholds(),
            metrics=store,
            tenant_id="T-1",
        )
        classify_with_jev(
            "问题",
            _Client(fails=JevUnavailable("down")),
            thresholds=RoutingThresholds(),
            metrics=store,
            tenant_id="T-1",
        )
    finally:
        routing.decide = original

    counts = _counts(store, "T-1")
    assert counts == {("routing", "completed"): 1, ("routing", "failed"): 1}, counts
    # And the rate is computable from those two numbers alone.
    completed = counts[("routing", ROUTING_DECIDED)]
    failed = counts.get(("routing", "failed"), 0)
    assert completed / (completed + failed) == 0.5


def test_no_client_records_nothing(tmp_path: Path) -> None:
    """No Jev configured is not a routing *outcome*.

    It is the feature being off, and counting it would report "decided" (or
    "failed") for a gateway where routing never ran at all — which is the
    mistake this ticket is about, in the other direction.
    """
    store = _store(tmp_path)
    result = classify_with_jev("问题", None, thresholds=RoutingThresholds(), metrics=store, tenant_id="T-1")
    assert result is None
    assert _counts(store, "T-1") == {}


def test_the_failure_code_is_unchanged(tmp_path: Path) -> None:
    """#405's criterion names `ROUTING_UNAVAILABLE`; it still does."""
    store = _store(tmp_path)
    import aiops_diagnostics.routing as routing

    original = routing.decide
    routing.decide = lambda question, client, *, thresholds: client.decide(question, thresholds)
    try:
        classify_with_jev(
            "问题",
            _Client(fails=JevUnavailable("down")),
            thresholds=RoutingThresholds(),
            metrics=store,
            tenant_id="T-1",
        )
    finally:
        routing.decide = original
    row = next(row for row in store.summary("T-1")["by_route"] if row["route_type"] == "routing")
    assert row["failed"] == 1, row
    # The failure row still carries its own code — the one #405's criterion names.
    recorded = _codes(store, "T-1")
    assert recorded == {ROUTING_UNAVAILABLE}, recorded


def _counts(store: MetricsStore, tenant_id: str) -> dict[tuple[str, str], int]:
    with store._connection() as connection:  # noqa: SLF001 - reading rows, not behaviour
        rows = connection.execute(
            "SELECT route_type, outcome, COUNT(*) AS n FROM agent_run_metrics"
            " WHERE tenant_id = ? GROUP BY route_type, outcome",
            (tenant_id,),
        ).fetchall()
    return {(str(row["route_type"]), str(row["outcome"])): int(row["n"]) for row in rows}


def _codes(store: MetricsStore, tenant_id: str) -> set[str]:
    with store._connection() as connection:  # noqa: SLF001
        rows = connection.execute(
            "SELECT error_code FROM agent_run_metrics WHERE tenant_id = ? AND outcome = 'failed'",
            (tenant_id,),
        ).fetchall()
    return {str(row["error_code"]) for row in rows}


def test_the_summary_can_answer_the_window_question(tmp_path: Path) -> None:
    """The operator-facing query #405 needs: "how many decisions in the window".

    `by_route` carries `completed` and `failed` per route type, which is the
    numerator and the denominator in one row — no new endpoint, no new name.
    """
    store = _store(tmp_path)
    import aiops_diagnostics.routing as routing

    original = routing.decide
    routing.decide = lambda question, client, *, thresholds: client.decide(question, thresholds)
    try:
        for _ in range(3):
            classify_with_jev(
                "问题",
                _Client(decision=_Decision({"risk": "low", "order_no": None})),
                thresholds=RoutingThresholds(),
                metrics=store,
                tenant_id="T-1",
            )
    finally:
        routing.decide = original

    routing_row = next(row for row in store.summary("T-1")["by_route"] if row["route_type"] == "routing")
    assert routing_row["completed"] == 3, routing_row
    assert routing_row["failed"] == 0, routing_row
    # And the interaction totals stay clean: routing is component health.
    assert store.summary("T-1")["totals"]["runs"] == 0
