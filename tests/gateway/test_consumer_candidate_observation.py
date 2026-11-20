"""D35控制端必须核对原持久任务、精确编排与停用入口。"""

import copy
import json
import time
from types import SimpleNamespace

import pytest
import yaml
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.dify import environment_consumer_trial as trial
from aiops_diagnostics.dify import environment_operator
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, MediaResourceSigner
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

    def export(_):
        workflow = copy.deepcopy(app["draft"])
        for variable in workflow.get("environment_variables", []):
            variable["selector"] = ["env", variable["name"]]
        return yaml.safe_dump({"workflow": workflow})

    monkeypatch.setattr(trial.DifyDslClient, "export_dsl", export)
    request = json.loads(_task_row(state, job["id"])["request"])
    value = {k: request[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id")}
    value.update(
        environment=request["plan"]["environment"],
        app_id=app["id"],
        task_id=job["id"],
        plan_id=request["plan"]["id"],
        order_no="",
        workflow_sha256=trial.candidate_workflow_sha(yaml.safe_dump({"workflow": app["draft"]})),
    )
    return state, context, value, app


def _task_row(state, identifier):
    path = state[0][0][0]
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        return dict(conn.execute("SELECT * FROM environment_tasks WHERE id=?", (identifier,)).fetchone())


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "tenant",
        "workspace",
        "library",
        "app",
        "plan",
        "digest",
        "variable",
        "enabled",
        "cancel",
        "expired",
    ],
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
    elif change == "variable":
        app["draft"]["environment_variables"][0]["value"] = "changed"
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


@pytest.mark.parametrize(
    "change",
    ["none", "cancel", "expired", "identity", "workflow", "failed", "missing", "scope", "final-cancel"],
)
@pytest.mark.parametrize("during_fetch", [False, True])
def test_trial_media_rechecks_lease_before_and_after_fetch(observed, monkeypatch, change, during_fetch):  # noqa: F811
    from consumer.test_candidate_trial import caller

    from aiops_diagnostics.dify import candidate_knowledge
    from aiops_diagnostics.execution.agent_runs import scope_to_json

    _, context, value, app = observed
    snapshot = trial.observed(context, value)
    status, current = ["completed"], [caller()]
    runtime = SimpleNamespace(
        media_signer=MediaResourceSigner("synthetic-media-secret"),
        kb_search_client=object(),
        gateway_settings=context.settings,
        _media_proxy=None,
        get_trial=lambda *_: {"status": status[0]},
    )
    context.runtime = runtime
    manager = trial.CandidateTrials(context)
    runtime.consumer_candidate_trials = manager
    adapter = trial.RemoteCandidate.__new__(trial.RemoteCandidate)
    adapter.context, adapter.scope, adapter.snapshot = context, value, snapshot
    adapter.caller, adapter.until, adapter.cancelled = caller(), time.monotonic() + 300, False
    adapter.call = lambda *_: {"candidate": value, "scope_json": scope_to_json(current[0]), "state": "closed"}
    manager.leases["trial-one"] = adapter
    scope = snapshot[4]
    resource = runtime.media_signer.issue(
        tenant_id=candidate_knowledge.namespace(scope),
        agent_version=candidate_knowledge.MEDIA_PREFIX + candidate_knowledge.knowledge_id(scope),
        session_id="trial-one",
        knowledge_base_id=scope[3],
        document_id="doc-one",
        chunk_id="chunk-one",
        backend_id="image-one",
        kind="image",
        mime_type="image/png",
        title="图片",
        reference_id="chunk-one",
    )
    fetched = []

    def revoke():
        if change == "cancel":

            def broken_cancel(*_):
                raise OSError("synthetic storage unavailable")

            runtime.cancel_trial = broken_cancel
            with pytest.raises(OSError):
                manager.read({"trial_id": "trial-one"}, cancel=True)
        elif change == "expired":
            adapter.until = 0
        elif change == "identity":
            current[0] = caller(user="other")
        elif change == "workflow":
            app["draft"]["environment_variables"][0]["value"] = "changed"
        elif change == "failed":
            status[0] = "failed"
        elif change == "missing":
            manager.forget("trial-one")
        elif change == "scope":
            adapter.snapshot = (*snapshot[:4], (*scope[:3], "other-kb"))
        elif change == "final-cancel":

            def cancel_on_readback(*_):
                adapter.cancelled = True
                return {"status": "completed"}

            runtime.get_trial = cancel_on_readback

    def fetch(*_, **kwargs):
        fetched.append(True)
        if during_fetch:
            revoke()
        return b"PNGDATA"

    monkeypatch.setattr(KbServiceClient, "fetch_media", fetch)
    if not during_fetch:
        revoke()
    response = GatewayRuntime.serve_media(runtime, resource.url.rsplit("/", 1)[-1])
    assert response.status_code == (200 if change == "none" else 403)
    assert response.body == (b"PNGDATA" if change == "none" else b"")
    assert len(fetched) == (1 if during_fetch or change == "none" else 0)
