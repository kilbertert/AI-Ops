"""D35控制端必须核对原持久任务、精确编排与停用入口。"""

import copy
import json
from types import SimpleNamespace

import pytest
import yaml
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.dify import environment_consumer_trial as trial
from aiops_diagnostics.dify import environment_operator
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import gateway_db
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app, execute, submit  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import prepared  # noqa: F401


@pytest.fixture
def observed(consumer_app, monkeypatch):  # noqa: F811
    state = consumer_app
    job = execute(state, submit(state).json()["id"])
    settings = state[0][2]
    context = SimpleNamespace(settings=settings)
    monkeypatch.setattr(trial, "DifyConsoleAdmin", environment_operator.DifyConsoleAdmin)
    app = state[3][job["result"]["app_id"]]
    monkeypatch.setattr(
        trial.DifyDslClient, "export_dsl", lambda _: yaml.safe_dump({"workflow": app["draft"]})
    )
    request = json.loads(_task_row(state, job["id"])["request"])
    value = {k: request[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id")}
    value.update(
        environment=request["plan"]["environment"],
        app_id=app["id"],
        task_id=job["id"],
        plan_id=request["plan"]["id"],
        order_no="",
        workflow_sha256=trial.workflow_of(trial.workflow_content(yaml.safe_dump({"workflow": app["draft"]}))),
    )
    return state, context, value, app


def _task_row(state, identifier):
    path = state[0][0][0]
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        return dict(conn.execute("SELECT * FROM environment_tasks WHERE id=?", (identifier,)).fetchone())


@pytest.mark.parametrize(
    "change",
    ["none", "tenant", "workspace", "library", "app", "plan", "digest", "enabled", "cancel", "expired"],
)
def test_exact_observation_and_drift_refusals(observed, change):  # noqa: F811
    state, context, value, app = observed
    before = copy.deepcopy(value)
    if change in {"tenant", "workspace", "app", "plan"}:
        value[
            {"tenant": "tenant_id", "workspace": "workspace_id", "app": "app_id", "plan": "plan_id"}[change]
        ] += "x"
    elif change == "library":
        value["knowledge_base_id"] = "other"
    elif change == "digest":
        value["workflow_sha256"] = "0" * 64
    elif change == "enabled":
        app["enable_api"] = True
    elif change in {"cancel", "expired"}:
        path = state[0][0][0]
        with gateway_db.connection(drafts._database(path), write=True) as db:
            if change == "cancel":
                db.execute("UPDATE environment_tasks SET cancel=1 WHERE id=?", (value["task_id"],))
            else:
                row = _task_row(state, value["task_id"])
                request = json.loads(row["request"])
                request["plan"]["expires_at"] = 0
                db.execute(
                    "UPDATE environment_tasks SET request=? WHERE id=?",
                    (json.dumps(request), value["task_id"]),
                )
    if change == "none":
        result = trial.observed(context, value)
        assert result[4] == tuple(
            value[k] for k in ("environment", "tenant_id", "workspace_id", "knowledge_base_id")
        )
        described = trial.CandidateTrials(context).describe({"task_id": value["task_id"], "order_no": ""})
        assert described["candidate"] == value and described["activated"] is False
    else:
        with pytest.raises((ValueError, RuntimeError, KeyError)):
            trial.observed(context, value)
    assert before["task_id"] == value["task_id"]
