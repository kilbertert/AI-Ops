from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "synthetic-acceptance"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 8, 30, 2, tzinfo=UTC)
    orders = []
    devices = []
    samples = []
    comm = []
    for index, scenario in enumerate(("complete", "partial", "forbidden"), start=1):
        tenant = "SYNTH-TENANT-A" if scenario != "forbidden" else "SYNTH-TENANT-B"
        order_no = f"SYNTH-ORDER-{index:03d}"
        device = f"SYNTH-DEVICE-{index:03d}"
        orders.append(_order(order_no, tenant, device, start, scenario))
        devices.append(
            {
                "id": f"SYNTH-ID-{index:03d}",
                "tenant_id": tenant,
                "site_id": "SYNTH-SITE-1",
                "device_code": device,
                "protocol": "OCPP",
                "status": 1,
            }
        )
        if scenario == "complete":
            for point in range(400):
                samples.append(_sample(device, start + timedelta(minutes=point), point))
            comm.append(
                {"_ts": start.isoformat(), "code": "0x25", "direction": 2, "decoded": {"synthetic": True}}
            )
    payload = {
        "orders": orders,
        "devices": devices,
        "fee_template_records": {},
        "gun_samples": samples,
        "comm_messages": comm,
        "streams": [],
    }
    (OUT / "synthetic_acceptance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "kind": "production-shaped-synthetic",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "local deterministic generator",
        "contains_production_data": False,
        "scenarios": [
            {
                "id": "SYN-COMPLETE",
                "order_no": "SYNTH-ORDER-001",
                "tenant_id": "SYNTH-TENANT-A",
                "expected": "complete telemetry",
            },
            {
                "id": "SYN-PARTIAL",
                "order_no": "SYNTH-ORDER-002",
                "tenant_id": "SYNTH-TENANT-A",
                "expected": "partial telemetry",
            },
            {
                "id": "SYN-FORBIDDEN",
                "order_no": "SYNTH-ORDER-003",
                "tenant_id": "SYNTH-TENANT-B",
                "expected": "outside caller scope",
            },
        ],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _order(order_no: str, tenant: str, device: str, start: datetime, scenario: str) -> dict[str, object]:
    return {
        "id": order_no,
        "order_no": order_no,
        "tenant_id": tenant,
        "status": 1,
        "type": 0,
        "device_id": device,
        "device_code": device,
        "child_device_code": device,
        "site_id": "SYNTH-SITE-1",
        "device_protocol": "OCPP",
        "created_time": start.isoformat(),
        "stop_time": (start + timedelta(minutes=399 if scenario == "complete" else 60)).isoformat(),
        "stopped_reason_code": "Local",
        "stopped_reason_content": "synthetic user stop",
        "is_receive_tx_data": 1 if scenario == "complete" else 0,
        "tx_data": {"synthetic": True},
    }


def _sample(device: str, at: datetime, point: int) -> dict[str, object]:
    return {
        "_ts": int(at.timestamp()),
        "device": device,
        "power": 20 + point % 30,
        "outputVoltage": 380 + point % 20,
        "outputCurrent": 50 + point % 10,
        "temperature": 25 + point % 8,
        "batteryMaxTemperature": 28 + point % 8,
        "batteryMinTemperature": 26 + point % 6,
        "soc": 20 + point * 0.1,
        "chargingElectricityQuantity": point * 0.05,
    }


if __name__ == "__main__":
    main()
