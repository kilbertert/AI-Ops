"""已执行试用的最终放行，以及撤回与新发布交错时的代次隔离。"""

import time
from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslSource
from aiops_diagnostics.dify.dify_workflow_trial import WorkflowTrial
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway.gateway_runtime import JobRegistration
from execution.test_app_runtime_dify_entry import ORDER_A, WORKFLOW_ID, _DifyStub, _Resolver, dify_chat

APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FORMAL = "22222222-2222-3333-4444-555555555555"
DSL = "app:\n  mode: workflow\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n"
TRIAL_HEADERS = {"Authorization": "Bearer synthetic-trial-key"}
PUBLISH_HEADERS = {"Authorization": "Bearer synthetic-publish-key"}


@pytest.fixture
def live_trial(tmp_path, monkeypatch):
    stub = _DifyStub()
    base = stub.start()
    try:
        with dify_chat(tmp_path, monkeypatch, stub_base=base) as f:
            runtime = f["app"].state.gateway.runtime
            scope = _Resolver().resolve("token-a", required_scope="x")
            settings = f["settings"]
            settings.chat_execution_bindings_file = ""
            settings.chat_route_bindings_file = str(f["db"])
            settings.dify_trial_api_key = "synthetic-trial-key"
            settings.dify_publish_api_key = "synthetic-publish-key"
            routes = ChatRouteBindingStore(f["db"])
            route = routes.publish(
                environment="env-a",
                tenant_id="TENANT-A",
                entry="consumer",
                scenario=chat_execution.SCENARIO_OWN_ORDER_STATUS,
                engine="dify",
                agent_id="agt_synthetic",
                agent_version_no=1,
                app_id=APP,
                workflow_id=FORMAL,
                workflow_key_ref=str(f["key_file"]),
            )
            runtime.dify_app_registry.put(DifyAppBinding("TENANT-A", "consumer", APP, "synthetic"))
            stub.gateway_base = f["gateway_base"]
            monkeypatch.setattr(runtime, "trial_run_context", lambda: scope)
            admin = SimpleNamespace(
                import_app_dsl=lambda *a, **kw: APP,
                app_service_key=lambda *a: "synthetic-key",
                delete_app=lambda *a: None,
            )
            console = SimpleNamespace(
                publish_workflow=lambda: True,
                published_workflow_id=lambda: WORKFLOW_ID,
                export_dsl=lambda **kw: DSL,
            )
            monkeypatch.setattr(
                "aiops_diagnostics.dify.dify_workflow_trial.DifyConsoleAdmin", lambda _: admin
            )
            monkeypatch.setattr("aiops_diagnostics.dify.dify_workflow_trial.DifyDslClient", lambda _: console)
            trial = WorkflowTrial(DSL, DifyDslSource(base, APP, "synthetic", "workspace"), route)
            row = runtime.start_trial("operator", ORDER_A, trial, "zh")
            trial_id = row["diagnosis_id"]
            url = "/v1/dify/trial/" + trial_id
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                response = f["client"].get(url, headers=TRIAL_HEADERS)
                if response.json().get("status") in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            assert response.status_code == 200 and response.json()["status"] == "completed", response.text
            assert f["upstream"]["calls"] == 1
            yield {
                **f,
                "runtime": runtime,
                "scope": scope,
                "routes": routes,
                "route": route,
                "trial_id": trial_id,
                "url": url,
            }
    finally:
        stub.stop()


def retract(f):
    result = f["client"].post("/v1/dify/retract", headers=PUBLISH_HEADERS, json={"app_id": APP})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize(
    "change", ["ownership", "retract", "republish", "expiry", "missing_run", "snapshot", "during_auth"]
)
def test_successful_trial_readback_rechecks_release(live_trial, monkeypatch, change):
    f = live_trial
    run = f["runtime"].dify_run_store.by_qa(f["trial_id"])
    if change == "ownership":
        f["orders"].ownership[ORDER_A] = "OTHER"
    elif change == "retract":
        retract(f)
    elif change == "republish":
        route = f["route"]
        f["routes"].publish(
            environment=route.environment,
            tenant_id=route.tenant_id,
            entry=route.entry,
            scenario=route.scenario,
            engine=route.engine,
            agent_id=route.agent_id,
            agent_version_no=1,
            app_id=APP,
            workflow_id=route.workflow_id,
            workflow_key_ref=route.workflow_key_ref,
            expected_revision=route.revision,
        )
    elif change == "during_auth":
        original = f["orders"].can_access

        def changed_during_auth(*args):
            result = original(*args)
            retract(f)
            return result

        monkeypatch.setattr(f["orders"], "can_access", changed_during_auth)
    else:
        with f["runtime"].dify_run_store._connection(write=True) as connection:
            if change == "expiry":
                connection.execute(
                    "UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
                    (run.run_id,),
                )
            elif change == "snapshot":
                connection.execute(
                    "UPDATE chat_dify_runs SET snapshot_json=NULL WHERE run_id=?", (run.run_id,)
                )
            else:
                connection.execute("DELETE FROM chat_dify_runs WHERE run_id=?", (run.run_id,))
    result = f["client"].get(f["url"], headers=TRIAL_HEADERS)
    assert result.status_code in {403, 404, 410, 503}, result.text
    assert "result" not in result.json()
    assert f["upstream"]["calls"] == 1


@pytest.mark.parametrize("new_version", [1, 2])
def test_old_retraction_keeps_new_generation_chat_and_trial(live_trial, monkeypatch, new_version):
    f = live_trial
    runtime = f["runtime"]
    scope = f["scope"]
    old = f["route"]
    stops = []

    def issue(prefix, route, kind):
        run_id = prefix + "-" + kind
        runtime.dify_run_store.issue(
            run_id=run_id,
            qa_id=run_id,
            scope_fingerprint=scope.scope_fingerprint,
            scope_json=scope_to_json(scope),
            binding_key=route.key,
            order_no=ORDER_A,
            environment=route.environment,
            tenant_id=route.tenant_id,
            app_id=APP,
            workflow_id=WORKFLOW_ID,
            grant_id=run_id,
            agent_version_no=route.agent_version_no,
            kind=kind,
            route_revision=route.revision,
        )
        runtime.dify_run_store.bind_workflow(run_id, workflow_run_id=run_id, task_id=run_id)
        runtime._qa_registrations[run_id] = JobRegistration(interrupt=lambda: stops.append(run_id))

    for kind in ("chat", "trial"):
        issue("old", old, kind)
    original = runtime.settle_retracted_runs

    def publish_after_retract_commit(*, route):
        assert route == old
        assert not f["routes"].by_key(old.key).active
        new = f["routes"].publish(
            environment=old.environment,
            tenant_id=old.tenant_id,
            entry=old.entry,
            scenario=old.scenario,
            engine=old.engine,
            agent_id=old.agent_id,
            agent_version_no=new_version,
            app_id=APP,
            workflow_id=WORKFLOW_ID,
            workflow_key_ref=old.workflow_key_ref,
        )
        for kind in ("chat", "trial"):
            issue("new", new, kind)
        return original(route=route)

    monkeypatch.setattr(runtime, "settle_retracted_runs", publish_after_retract_commit)
    retract(f)
    assert sorted(stops) == ["old-chat", "old-trial"]
    for kind in ("chat", "trial"):
        assert runtime.dify_run_store.get("old-" + kind).state == "cancelled"
        assert runtime.dify_run_store.get("new-" + kind).state == "running"
    assert f["routes"].by_key(old.key).active
