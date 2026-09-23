"""The calibration's arithmetic, which the report's recommendation rests on.

The report argues for a threshold pair from a sweep of the measured values. The
sweep is code, so it is testable — and it should be, because the whole point of
#390 is that the threshold must follow from measurement rather than intuition.
These tests use synthetic rows; they check the *rule*, not the measured data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[1] / "tools" / "jev_calibration.py"


def _load():
    """Import the script as a module (it lives in tools/, not the package).

    The module must be registered in ``sys.modules`` *before* it executes: its
    dataclasses use ``slots=True``, and ``dataclasses`` resolves the defining
    module through ``sys.modules`` while building the class.
    """
    import sys

    spec = importlib.util.spec_from_file_location("jev_calibration", _MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["jev_calibration"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cal():
    return _load()


def _row(cal, question: str, *, risk: float, confidence: float):
    return cal.Row(
        question=question,
        expected="knowledge",
        note="synthetic",
        jev_risk=risk,
        jev_confidence=confidence,
    )


def test_the_shipped_rule_is_reproduced_on_continuous_values(cal) -> None:
    """`risk high AND confidence not high` becomes `>= risk AND < confidence`.

    The strict inequality on confidence is the part worth pinning: at the
    boundary the rule must *not* ask, exactly as `confidence == "high"` does not
    ask today.
    """
    rows = [
        _row(cal, "risky and unsure", risk=0.9, confidence=0.5),
        _row(cal, "risky and sure", risk=0.9, confidence=0.9),
        _row(cal, "safe and unsure", risk=0.1, confidence=0.5),
    ]
    swept = cal.sweep_thresholds(rows, risk_steps=(0.5,), confidence_steps=(0.9,))
    assert len(swept) == 1
    assert swept[0]["asked_questions"] == ["risky and unsure"]


def test_the_boundary_confidence_does_not_ask(cal) -> None:
    """A confidence exactly at the threshold is 'high enough', not 'short of it'."""
    rows = [_row(cal, "exactly at the line", risk=0.9, confidence=0.8)]
    swept = cal.sweep_thresholds(rows, risk_steps=(0.5,), confidence_steps=(0.8,))
    assert swept[0]["would_ask"] == 0


def test_the_sweep_is_a_full_grid(cal) -> None:
    swept = cal.sweep_thresholds([], risk_steps=(0.3, 0.4), confidence_steps=(0.6, 0.7, 0.8))
    assert len(swept) == 6
    assert {(item["risk_at_least"], item["confidence_at_least"]) for item in swept} == {
        (0.3, 0.6),
        (0.3, 0.7),
        (0.3, 0.8),
        (0.4, 0.6),
        (0.4, 0.7),
        (0.4, 0.8),
    }


def test_a_row_missing_a_jev_value_is_left_out_of_the_sweep(cal) -> None:
    """A failed call must not be silently counted as 'did not ask'."""
    rows = [
        _row(cal, "measured", risk=0.9, confidence=0.5),
        cal.Row(question="failed", expected="knowledge", note="", jev_error="boom"),
    ]
    swept = cal.sweep_thresholds(rows, risk_steps=(0.5,), confidence_steps=(0.8,))
    assert swept[0]["would_ask"] == 1
    assert swept[0]["asked_questions"] == ["measured"]


def test_the_report_carries_the_sweep_and_the_rows(cal, tmp_path) -> None:
    """The emitted report must be self-contained: numbers plus their provenance."""
    import json

    rows = [
        json.loads(line)
        for line in [
            '{"question":"q","expected_by_author":"casual","note":"n",'
            '"baseline":{"intent":"casual","risk":"low","confidence":"high","seconds":1.0,"error":null},'
            '"jev":{"intent":"casual","confidence":1.0,"risk":0.02,"seconds":0.6,"error":null}}'
        ]
    ]
    report = {
        "summary": cal._summarize([_row(cal, "q", risk=0.02, confidence=1.0)]),
        "threshold_sweep": cal.sweep_thresholds([_row(cal, "q", risk=0.02, confidence=1.0)]),
        "rows": rows,
    }
    text = json.dumps(report, ensure_ascii=False)
    assert "threshold_sweep" in text and "rows" in text
