from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "dify_ops_model_evidence",
    Path(__file__).parents[2] / "tools/verification/verify_dify_ops_model_evidence.py",
)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


@pytest.fixture
def evidence():
    results = {
        "order_list": {
            "orders": [{"order_no": "O-1"}],
            "page": 1,
            "page_size": 1,
            "source": "mysql.ch_order_info",
        },
        "charging_info": {
            "order_no": "O-1",
            "status_code": "1",
            "status_role": "finished",
            "source": "cpms.chargingInfo",
            "observed_at": "2026-10-22T10:38:19Z",
        },
    }
    events = []
    audit = []
    for position, (tool, result) in enumerate(results.items(), 1):
        row = {
            "call_id": f"call-{position}",
            "tool": tool,
            "outcome": "succeeded",
            "subject_type": "tenant_service",
            "tenant_id": "T-1",
            "registered_app_id": "app-1",
            "registered_workspace_id": "ws-1",
            "permission_version": 19,
            "result_sha256": module.digest(result),
        }
        output = json.dumps({"result": result, "evidence": row})
        events.append(
            {
                "event": "agent_thought",
                "id": f"thought-{position}",
                "position": position,
                "tool": tool,
                "tool_input": json.dumps(
                    {tool: {"page": 1, "page_size": 1} if position == 1 else {"order_no": "O-1"}}
                ),
                "observation": json.dumps({tool: output + output}),
            }
        )
        audit.append(row)
    events.append({"event": "agent_thought", "id": "thought-3", "position": 3})
    final = {**results["charging_info"], "call_id": "call-2"}
    events.append(
        {"event": "agent_message", "answer": "<think>private reasoning</think>" + json.dumps(final)}
    )
    events.append(
        {
            "event": "message_end",
            "task_id": "task-1",
            "message_id": "msg-1",
            "conversation_id": "conv-1",
            "metadata": {"usage": {"total_tokens": 123}},
        }
    )
    return events, audit


def verify(evidence):
    return module.verify(*evidence, tenant="T-1", app="app-1", workspace="ws-1", version=19)


def test_accepts_identical_concatenated_results_and_keeps_two_calls(evidence):
    report = verify(evidence)
    assert report["passed"] and report["successful_tool_calls"] == 2
    assert report["duplicate_results_ignored"] == 2 and report["reasoning_prefix_present"]
    assert "O-1" not in json.dumps(report) and "private reasoning" not in json.dumps(report)


def test_conflicting_result_with_the_same_call_id_is_refused(evidence):
    events, _ = evidence
    outer = json.loads(events[0]["observation"])
    first = next(module.json_values(outer["order_list"]))
    second = copy.deepcopy(first)
    second["result"]["orders"][0]["order_no"] = "O-2"
    second["evidence"]["result_sha256"] = module.digest(second["result"])
    events[0]["observation"] = json.dumps({"order_list": json.dumps(first) + json.dumps(second)})
    with pytest.raises(AssertionError, match="conflicting repeated"):
        verify(evidence)


def test_malformed_trailing_result_is_not_ignored(evidence):
    events, _ = evidence
    outer = json.loads(events[0]["observation"])
    outer["order_list"] += "not-json"
    events[0]["observation"] = json.dumps(outer)
    with pytest.raises(ValueError):
        verify(evidence)


def test_result_cannot_move_to_another_decision(evidence):
    events, _ = evidence
    events[0]["observation"], events[1]["observation"] = events[1]["observation"], events[0]["observation"]
    with pytest.raises(AssertionError, match="another decision"):
        verify(evidence)


def test_cli_failure_does_not_print_private_evidence(evidence, tmp_path):
    events, audit = evidence
    events.pop()
    paths = [tmp_path / name for name in ("events.json", "audit.json", "report.json")]
    for path, data in zip(paths[:2], (events, audit), strict=True):
        path.write_text(json.dumps(data))
    result = subprocess.run(
        [
            sys.executable,
            str(spec.origin),
            "--events",
            str(paths[0]),
            "--audit",
            str(paths[1]),
            "--tenant",
            "T-1",
            "--app",
            "app-1",
            "--workspace",
            "ws-1",
            "--version",
            "19",
            "--out",
            str(paths[2]),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and "FAIL: AssertionError" in result.stdout
    assert "O-1" not in result.stdout + result.stderr
    assert "private reasoning" not in result.stdout + result.stderr and not paths[2].exists()


@pytest.mark.parametrize(
    "failure", ["missing_end", "wrong_order", "wrong_answer", "extra_audit", "mixed_identity"]
)
def test_incomplete_or_unbound_evidence_cannot_pass(evidence, failure):
    events, audit = evidence
    if failure == "missing_end":
        events.pop()
    elif failure == "wrong_order":
        events[1]["tool_input"] = json.dumps({"charging_info": {"order_no": "O-2"}})
    elif failure == "wrong_answer":
        events[-2]["answer"] = events[-2]["answer"].replace('"finished"', '"charging"')
    elif failure == "extra_audit":
        audit.append({**audit[0], "call_id": "extra-call"})
    else:
        events[0]["task_id"] = "another-task"
    with pytest.raises(AssertionError):
        verify(evidence)
