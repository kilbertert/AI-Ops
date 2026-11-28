"""验证 AI-Ops 调用 fleet-kit 时保留文档巡检台账的并发约束。"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/doc-lifecycle.yml"


def test_all_autovacuum_triggers_share_one_non_canceling_queue() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert {"schedule", "workflow_dispatch"} <= triggers.keys()

    audit = workflow["jobs"]["autovacuum"]
    assert audit["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    assert audit["concurrency"] == {
        "group": "doc-lifecycle-maintenance-ledger",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_pr_checks_cancel_only_older_runs_for_the_same_pr() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "concurrency" not in workflow  # The outer workflow must not replace queued audits.
    assert workflow["jobs"]["pr-check"]["concurrency"] == {
        "group": "doc-lifecycle-pr-${{ github.ref }}",
        "cancel-in-progress": True,
    }
