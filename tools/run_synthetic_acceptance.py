from __future__ import annotations

import json
from pathlib import Path

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_metrics import enrich_report
from aiops_diagnostics.health_report import build_minimal_health_report
from aiops_diagnostics.sources import FixtureSources

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples" / "synthetic-acceptance" / "synthetic_acceptance.json"


def main() -> None:
    source = FixtureSources(DATA)
    complete = _report(source, "SYNTH-ORDER-001")
    partial = _report(source, "SYNTH-ORDER-002")
    forbidden = _report(source, "SYNTH-ORDER-003", tenant_id="SYNTH-TENANT-A")
    assert len(complete["curves"]["power"]["series"][0]["points"]) == 300
    assert complete["curves"]["power"]["original_points"] == 400
    assert partial["source_summary"]["telemetry"] == "unavailable"
    assert forbidden == {"status": "rejected", "code": "ORDER_NOT_FOUND"}
    print(
        json.dumps(
            {
                "status": "passed",
                "scenarios": {"complete": complete, "partial": partial, "forbidden": forbidden},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _report(source: FixtureSources, order_no: str, tenant_id: str | None = None) -> dict:
    orders = source.get_orders(order_no, tenant_id)
    if not orders:
        return {"status": "rejected", "code": "ORDER_NOT_FOUND"}
    report = build_minimal_health_report(source, order_no, SafetySettings())
    order = orders[0]
    samples = source.get_gun_samples(order["device_code"], None, None, None)
    report["curves"] = build_curves(samples if order_no.endswith("001") else [])
    report["source_summary"]["telemetry"] = (
        "available" if samples and order_no.endswith("001") else "unavailable"
    )
    return enrich_report(report, samples if order_no.endswith("001") else [], order)


if __name__ == "__main__":
    main()
