#!/usr/bin/env python3
"""Re-measure the routing thresholds against real traffic (PRD #383 follow-up).

The first calibration (#390) used ten hand-written questions. It produced the
shipped pair (`risk >= 0.5`, `confidence < 0.8`) and said plainly that n=10 was
not enough to settle it. This script re-measures against questions that real
users actually asked, which is the only sample that can answer the question the
first one could not.

What it reports, and why each part matters:

* **The risk distribution.** The shipped rule is asymmetric — "ask rather than
  act on a thin judgement about something involving money" — so the risk
  threshold decides *which questions are treated as involving money*. If the
  real distribution is not bimodal, a midpoint is not obviously right and the
  report has to say so instead of picking one.
* **Where the confidence threshold sits relative to observed values.** #390
  measured a run-to-run drift of 0.71 → 0.67 on one question. A threshold near
  an observed value makes the same question behave differently on different
  days, so the report shows the distance from the nearest observation.
* **What each candidate pair would do to this corpus** — how many questions get
  asked for context, and which.
* **The degenerate inputs.** Real traffic includes ASR fragments ("喂喂喂喂喂喂。",
  "蹦极。", "123") and mixed-language text. These are where a classifier is most
  likely to produce something odd, so they are reported individually rather than
  averaged away.

    PYTHONPATH=src python tools/jev_recalibrate.py \
        --corpus docs/jev-recalibration-corpus.json \
        --jev-base-url https://api.commandcode.ai/provider/v1 \
        --jev-key "$JEV_KEY" --out docs/jev-recalibration.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.jev_decisions import (
    ChoiceAnswer,
    JevDecisionClient,
    JevSettings,
    Noul,
    NoulAnswer,
)
from aiops_diagnostics.routing import INTENT_CRITERIA, RoutingThresholds
from aiops_diagnostics.routing import Choice as RoutingChoice

#: The pair currently deployed. The re-measurement exists to confirm or move it.
SHIPPED = RoutingThresholds()

_RISK_STEPS: tuple[float, ...] = (0.3, 0.4, 0.5, 0.6, 0.7)
_CONFIDENCE_STEPS: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9)


def measure(question: str, client: JevDecisionClient) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    decision = client.decide(
        question,
        {
            "intent": RoutingChoice(
                instructions="用户这句话属于哪一类意图？只能选一个。",
                criteria=INTENT_CRITERIA,
            ),
            "risk": Noul(instructions="这句话是否涉及高风险、需要人工确认的扣费、资金或投诉问题？"),
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


def _band(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p25": round(statistics.quantiles(values, n=4)[0], 4) if len(values) >= 4 else None,
        "median": round(statistics.median(values), 4),
        "p75": round(statistics.quantiles(values, n=4)[2], 4) if len(values) >= 4 else None,
        "max": round(max(values), 4),
    }


def sweep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable = [r for r in rows if r["risk"] is not None and r["confidence"] is not None]
    out = []
    for risk_at_least in _RISK_STEPS:
        for confidence_at_least in _CONFIDENCE_STEPS:
            asked = [
                r["question"]
                for r in usable
                if r["risk"] >= risk_at_least and r["confidence"] < confidence_at_least
            ]
            out.append(
                {
                    "risk_at_least": risk_at_least,
                    "confidence_at_least": confidence_at_least,
                    "would_ask": len(asked),
                    "asked": asked,
                }
            )
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [r for r in rows if r["risk"] is not None and r["confidence"] is not None]
    risks = [r["risk"] for r in usable]
    confs = [r["confidence"] for r in usable]
    # The spread of the risk signal is the whole question: a bimodal sample makes
    # a midpoint threshold safe, a continuous one does not.
    high = [r for r in usable if r["risk"] >= SHIPPED.risk_at_least]
    low = [r for r in usable if r["risk"] < SHIPPED.risk_at_least]
    summary: dict[str, Any] = {
        "questions": len(rows),
        "decided": len(usable),
        "errors": len(rows) - len(usable),
        "risk": _band(risks),
        "confidence": _band(confs),
        "risk_high_count": len(high),
        "risk_low_count": len(low),
        # The gap the shipped threshold sits in, if there is one.
        "risk_gap": (
            round(min(r["risk"] for r in high) - max(r["risk"] for r in low), 4) if high and low else None
        ),
        "nearest_confidence_to_threshold": (
            round(
                min((abs(c - SHIPPED.confidence_at_least), c) for c in confs)[1],
                4,
            )
            if confs
            else None
        ),
        "distance_to_nearest_confidence": (
            round(min(abs(c - SHIPPED.confidence_at_least) for c in confs), 4) if confs else None
        ),
        "would_ask_now": sum(
            1
            for r in usable
            if r["risk"] >= SHIPPED.risk_at_least and r["confidence"] < SHIPPED.confidence_at_least
        ),
        "latency_seconds": _band([r["seconds"] for r in rows if r.get("seconds")]),
    }
    intents: dict[str, int] = {}
    for r in usable:
        intents[r["intent"]] = intents.get(r["intent"], 0) + 1
    summary["intents"] = dict(sorted(intents.items(), key=lambda kv: -kv[1]))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--jev-base-url", required=True)
    parser.add_argument("--jev-key", required=True)
    parser.add_argument("--jev-model", default="typesafe/jev")
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    questions = json.loads(args.corpus.read_text(encoding="utf-8"))["questions"]
    if args.limit:
        questions = questions[: args.limit]

    client = JevDecisionClient(
        JevSettings(base_url=args.jev_base_url, api_key=args.jev_key, model=args.jev_model)
    )
    rows: list[dict[str, Any]] = []
    for index, question in enumerate(questions, 1):
        row: dict[str, Any] = {"question": question, "intent": None, "confidence": None, "risk": None}
        try:
            decided, seconds = measure(question, client)
            row.update(decided, seconds=round(seconds, 3))
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the sample
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        rows.append(row)
        print(
            f"  [{index:>3}/{len(questions)}] {question[:30]:<32} "
            f"{str(row['intent']):<18} c={row['confidence']} r={row['risk']}",
            file=sys.stderr,
        )

    report = {
        "thresholds_shipped": {
            "risk_at_least": SHIPPED.risk_at_least,
            "confidence_at_least": SHIPPED.confidence_at_least,
        },
        "summary": summarize(rows),
        "sweep": sweep(rows),
        "rows": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
