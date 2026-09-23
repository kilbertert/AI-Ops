#!/usr/bin/env python3
"""Threshold calibration for the Jev routing decision (PRD #383, ticket #390).

The routing rule this repository ships today is asymmetric:

    risk == "high" AND confidence != "high"  →  ask the user for context

The intent is "ask one more question rather than act on a thin judgement about
something involving money". Jev answers with a continuous confidence instead of
three buckets, so that rule has to be re-expressed as a threshold — and the
threshold has to come from measurement, not intuition. "0.7" is not "high".

This script measures, for one batch of real questions:

* what the existing classifier decides (`intent`, `risk`, `confidence`),
* what Jev decides for the same question,
* whether the two agree on `intent`,
* the distribution of Jev's `confidence` and `risk`, split by whether the
  existing classifier put the question in the "must ask" bucket,
* and end-to-end latency for both.

Then it reports which thresholds would reproduce the existing rule's behaviour
for this batch, and — more usefully — where the two disagree, because a
disagreement is either a Jev mistake, an existing-classifier mistake, or a labell
that was always arguable.

Run it with a config that has a model provider (the baseline classifier needs
one) and Jev credentials:

    PYTHONPATH=src python tools/jev_calibration.py \
        --config ~/.config/aiops-diagnostics/repro-deepseek.env \
        --jev-base-url https://api.commandcode.ai/provider/v1 \
        --jev-key "$JEV_KEY" \
        --out docs/jev-threshold-calibration.json

Nothing here is asserted automatically. The script reports; a person decides
whether the numbers support switching routing over, and that decision belongs in
the report it emits, not in this file.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

#: Real questions, chosen to cover all six intents and the boundaries that
#: matter for the threshold: clear chit-chat, clear business, complaints that
#: should read as high risk, and the "thin judgement" band in between.
#: `expect` is the author's reading, recorded so a disagreement can be judged —
#: it is NOT a ground truth the models are graded against, because several of
#: these are genuinely arguable and a report that hides that is not useful.
QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("你好，你好，你好。", "casual", "重复寒暄"),
    ("谢谢", "casual", "短寒暄"),
    ("Hello, my car is not working.", "order_issue", "英文故障描述（标签本身可争议）"),
    ("充电桩怎么拔枪？有没有演示视频", "knowledge", "业务知识"),
    ("下暴雨、打雷或路面积水时，可以在户外露天充电吗？", "knowledge", "安全知识"),
    ("优惠券使用规则是什么？为什么结账时没有抵扣？", "knowledge", "计费知识（可能涉高风险）"),
    ("我想看看客户案例", "case_exploration", "宣传-案例"),
    ("你们有没有针对物流行业的方案", "solution_discovery", "宣传-方案"),
    ("我要投诉，充电扣了我200块钱但没充上电", "report_fault", "投诉（高风险）"),
    ("扣费不对，多扣了我50元，订单号2096164064667852801", "order_issue", "订单异常（高风险）"),
)

INTENT_CRITERIA = {
    "knowledge": "询问充电/新能源业务知识（政策、操作、产品用法）",
    "casual": "寒暄、闲聊或问候，不需要业务知识",
    "order_issue": "涉及某个具体订单的异常（金额、电量、故障）",
    "report_fault": "用户要上报一个故障或投诉",
    "case_exploration": "用户想看客户案例",
    "solution_discovery": "用户想看行业方案或解决方案",
}


@dataclass(frozen=True, slots=True)
class Row:
    question: str
    expected: str
    note: str
    baseline_intent: str | None = None
    baseline_risk: str | None = None
    baseline_confidence: str | None = None
    baseline_seconds: float | None = None
    baseline_error: str | None = None
    jev_intent: str | None = None
    jev_confidence: float | None = None
    jev_risk: float | None = None
    jev_seconds: float | None = None
    jev_error: str | None = None

    @property
    def agrees(self) -> bool | None:
        if self.baseline_intent is None or self.jev_intent is None:
            return None
        return self.baseline_intent == self.jev_intent

    @property
    def baseline_asks(self) -> bool | None:
        """The existing rule's verdict: would it stop and ask for context?"""
        if self.baseline_risk is None or self.baseline_confidence is None:
            return None
        return self.baseline_risk == "high" and self.baseline_confidence != "high"

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "expected_by_author": self.expected,
            "note": self.note,
            "baseline": {
                "intent": self.baseline_intent,
                "risk": self.baseline_risk,
                "confidence": self.baseline_confidence,
                "seconds": self.baseline_seconds,
                "error": self.baseline_error,
            },
            "jev": {
                "intent": self.jev_intent,
                "confidence": self.jev_confidence,
                "risk": self.jev_risk,
                "seconds": self.jev_seconds,
                "error": self.jev_error,
            },
            "intent_agrees": self.agrees,
            "baseline_would_ask": self.baseline_asks,
        }


def _baseline(question: str, config: Path, language: str) -> tuple[dict[str, Any], float]:
    """One call to the shipping classifier, exactly as the gateway makes it."""
    from aiops_diagnostics.agent_runner import classify_lightweight
    from aiops_diagnostics.config import Settings

    settings = Settings.from_config(config)
    settings.agent.run_root = "/tmp/jev-calibration-runs"
    provider = settings.agent.select_provider(None)
    started = time.monotonic()
    result = classify_lightweight(question, settings, provider=provider.name, language=language)
    return result, time.monotonic() - started


def _jev(question: str, base_url: str, key: str, model: str) -> tuple[dict[str, Any], float]:
    from aiops_diagnostics.jev_decisions import (
        Choice,
        ChoiceAnswer,
        JevDecisionClient,
        JevSettings,
        Noul,
        NoulAnswer,
    )

    client = JevDecisionClient(
        JevSettings(base_url=base_url, api_key=key, model=model, timeout=30.0),
    )
    started = time.monotonic()
    decision = client.decide(
        question,
        {
            "intent": Choice(
                instructions="用户这句话属于哪一类意图？只能选一个。",
                criteria=INTENT_CRITERIA,
            ),
            "risk": Noul(
                instructions="这句话是否涉及高风险、需要人工确认的扣费、资金或投诉问题？",
            ),
        },
    )
    elapsed = time.monotonic() - started
    intent = decision.answers.get("intent")
    risk = decision.answers.get("risk")
    return (
        {
            "intent": intent.choice if isinstance(intent, ChoiceAnswer) else None,
            "confidence": intent.confidence if isinstance(intent, ChoiceAnswer) else None,
            "risk": risk.noul if isinstance(risk, NoulAnswer) else None,
        },
        elapsed,
    )


def sweep_thresholds(
    rows: list[Row],
    *,
    risk_steps: tuple[float, ...] = (0.3, 0.4, 0.5, 0.6, 0.7),
    confidence_steps: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9),
) -> list[dict[str, Any]]:
    """Every risk/confidence pair, with what it would do to this batch.

    The shipped rule is ``risk high AND confidence not high``; expressed on Jev's
    continuous values that becomes ``risk >= risk_at_least AND confidence <
    confidence_at_least``. This enumerates the pairs rather than picking one, so
    the choice is visible and can be argued with — a single number in a report
    invites the reader to trust it without seeing what it costs.
    """
    usable = [row for row in rows if row.jev_risk is not None and row.jev_confidence is not None]
    out: list[dict[str, Any]] = []
    for risk_at_least in risk_steps:
        for confidence_at_least in confidence_steps:
            asked = [
                row.question
                for row in usable
                if row.jev_risk >= risk_at_least and row.jev_confidence < confidence_at_least
            ]
            out.append(
                {
                    "risk_at_least": risk_at_least,
                    "confidence_at_least": confidence_at_least,
                    "would_ask": len(asked),
                    "asked_questions": asked,
                }
            )
    return out


def _summarize(rows: list[Row]) -> dict[str, Any]:
    usable = [row for row in rows if row.baseline_confidence is not None and row.jev_confidence is not None]
    ask = [row for row in usable if row.baseline_asks]
    no_ask = [row for row in usable if row.baseline_asks is False]

    def _band(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "median": round(statistics.median(values), 4),
        }

    summary: dict[str, Any] = {
        "questions": len(rows),
        "both_decided": len(usable),
        "intent_agreements": sum(1 for row in usable if row.agrees),
        "intent_disagreements": sum(1 for row in usable if row.agrees is False),
        "baseline_would_ask": len(ask),
        "jev_confidence_when_baseline_asks": _band(
            [row.jev_confidence for row in ask if row.jev_confidence is not None]
        ),
        "jev_confidence_when_baseline_does_not_ask": _band(
            [row.jev_confidence for row in no_ask if row.jev_confidence is not None]
        ),
        "jev_risk_when_baseline_asks": _band([row.jev_risk for row in ask if row.jev_risk is not None]),
        "jev_risk_when_baseline_does_not_ask": _band(
            [row.jev_risk for row in no_ask if row.jev_risk is not None]
        ),
    }
    latencies = [row.jev_seconds for row in rows if row.jev_seconds is not None]
    if latencies:
        summary["jev_latency_seconds"] = {
            "min": round(min(latencies), 3),
            "median": round(statistics.median(latencies), 3),
            "max": round(max(latencies), 3),
        }
    base_latencies = [row.baseline_seconds for row in rows if row.baseline_seconds is not None]
    if base_latencies:
        summary["baseline_latency_seconds"] = {
            "min": round(min(base_latencies), 3),
            "median": round(statistics.median(base_latencies), 3),
            "max": round(max(base_latencies), 3),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="config with a model provider")
    parser.add_argument("--jev-base-url", required=True)
    parser.add_argument("--jev-key", required=True)
    parser.add_argument("--jev-model", default="typesafe/jev")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--out", type=Path, default=None, help="write the full report here")
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Jev only; useful when no model provider is reachable",
    )
    args = parser.parse_args(argv)

    rows: list[Row] = []
    for question, expected, note in QUESTIONS:
        row = Row(question=question, expected=expected, note=note)
        if not args.skip_baseline:
            try:
                result, seconds = _baseline(question, args.config, args.language)
                row = replace(
                    row,
                    baseline_intent=result.get("intent"),
                    baseline_risk=result.get("risk"),
                    baseline_confidence=result.get("confidence"),
                    baseline_seconds=round(seconds, 3),
                )
            except (ValueError, RuntimeError, OSError) as exc:
                row = replace(row, baseline_error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            decided, seconds = _jev(question, args.jev_base_url, args.jev_key, args.jev_model)
            row = replace(
                row,
                jev_intent=decided["intent"],
                jev_confidence=decided["confidence"],
                jev_risk=decided["risk"],
                jev_seconds=round(seconds, 3),
            )
        except Exception as exc:  # noqa: BLE001 - a report must survive one bad row
            row = replace(row, jev_error=f"{type(exc).__name__}: {exc}"[:200])
        rows.append(row)
        print(
            f"  {question[:26]:<28} base={row.baseline_intent or '-':<17}"
            f" jev={row.jev_intent or '-':<17} agree={row.agrees}",
            file=sys.stderr,
        )

    report = {
        "summary": _summarize(rows),
        "threshold_sweep": sweep_thresholds(rows),
        "rows": [row.as_dict() for row in rows],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
