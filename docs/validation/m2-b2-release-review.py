import argparse
import hashlib
import json
import runpy
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument("--out", type=Path, default=Path("/tmp/aiops-b2-release-review.json"))
args = parser.parse_args()
repo = args.repo.resolve()
sys.path.insert(0, str(repo / "src"))
t = runpy.run_path(str(repo / "tests/test_m2_close_contract.py"))
results = []
for case in [
    "expected_node_rebind_final_authorization",
    "denied_cached_then_restored",
    "unavailable_cached_then_restored",
]:
    with (
        tempfile.TemporaryDirectory(prefix="aiops-b2-review-") as directory,
        pytest.MonkeyPatch.context() as mp,
    ):
        f, run_id = t["_succeed_run"](Path(directory), mp)
        try:
            if case == "expected_node_rebind_final_authorization":

                def change(fixture=f):
                    path = Path(fixture["config"])
                    raw = json.loads(path.read_text())
                    raw["expected_node_id"] = "node-a2"
                    path.write_text(json.dumps(raw))

                response, hooks = t["_claim_with_mutation"](f, run_id, change)
                record = {
                    "case": case,
                    "status": response.status_code,
                    "hooks": hooks,
                    "released_order": response.json().get("order_no"),
                }
            else:
                if case.startswith("denied"):
                    f["orders"].ownership["ORDER-A"] = "USER-ELSEWHERE"
                else:
                    f["orders"].unavailable = True
                denied = t["_claim"](f, run_id)
                after_denial = f["store"].get(run_id).state
                f["orders"].ownership["ORDER-A"] = "USER-A"
                f["orders"].unavailable = False
                retried = t["_claim"](f, run_id)
                record = {
                    "case": case,
                    "first_status": denied.status_code,
                    "state_after_refusal": after_denial,
                    "retry_status": retried.status_code,
                    "released_order": retried.json().get("order_no"),
                }
            results.append(record)
        finally:
            f["client"].close()
            f["_upstream_ctx"].__exit__(None, None, None)
report = {
    "timestamp_utc": datetime.now(UTC).isoformat(),
    "code_identity": {
        "src/aiops_diagnostics/gateway_api.py": hashlib.sha256(
            (repo / "src/aiops_diagnostics/gateway_api.py").read_bytes()
        ).hexdigest()
    },
    "results": results,
}
args.out.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
