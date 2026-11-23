"""D29：消费者候选明确模板、复合单库、停用导入与未知结果恢复。"""

import copy
import json
import time
from pathlib import Path
from uuid import UUID, uuid5

import pytest
import yaml
from consumer.test_consumer_agent_flow import candidate
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.dify import environment_consumer as consumer
from aiops_diagnostics.dify import environment_operator as applications
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentStore
from gateway.test_candidate_knowledge_connection import (  # noqa: F401
    connected,
    prepare_connection,
    request_job,
)
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import prepared  # noqa: F401


@pytest.fixture
def consumer_app(connected, tmp_path, monkeypatch):  # noqa: F811
    fixture, _, settings, apis, *_ = connected
    parent = prepare_connection(connected, request_job(connected))
    source = {"app_id": "source-app", "name": "正式客服", "version_no": 7, "workflow": candidate()}
    selected = source["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["model"]["value"]
    secret = tmp_path / "model.json"
    secret.write_text('{"api_key":"synthetic"}')
    secret.chmod(0o600)
    approved = {"provider": selected["provider"], "name": selected["model"], "credential_file": str(secret)}
    monkeypatch.setattr(consumer, "profile", lambda *_: {"model": approved})

    def sources(_, caller, app_id=None):
        if not caller.platform or app_id != source["app_id"]:
            raise auth.SetupRefused("SETUP_SOURCE_NOT_AVAILABLE")
        return copy.deepcopy(source)

    monkeypatch.setattr(consumer, "sources", sources)
    monkeypatch.setattr(consumer.self_service_prepare, "model_credentials", lambda *_: None)
    apps, calls, credentials, lose = {}, [], [], [False]
    ws, library = parent["result"]["workspace_id"], parent["result"]["knowledge_base_id"]

    class Console:
        def __init__(self, _):
            pass

        def install_plugins(self, workspace, plugins):
            assert workspace == ws and len(plugins) == 2

        def _call(self, workspace, path, method="GET", body=None, **kwargs):
            assert workspace == ws
            calls.append((method, path))
            if "/datasets/external-knowledge-api/" in path:
                return apis[0]
            if path == "/console/api/apps/imports":
                if method == "GET":
                    return {"aiops_idempotent_import": 1, "aiops_candidate_chatflow_import": 1}
                doc = yaml.safe_load(body["yaml_content"])
                assert doc["aiops_candidate"] is True
                identifier = str(
                    uuid5(
                        UUID(applications.self_service_prepare.IMPORT_NAMESPACE),
                        ws + ":" + body["request_id"],
                    )
                )
                apps[identifier] = {
                    "id": identifier,
                    "name": body["name"],
                    "mode": "advanced-chat",
                    "enable_api": False,
                    "enable_site": False,
                    "draft": doc["workflow"],
                }
                if lose[0]:
                    raise ConsoleApiError("DIFY_CONSOLE_UNREACHABLE", "lost")
                return {"app_id": identifier, "status": "completed"}
            if path.startswith("/console/api/apps/"):
                identifier = path.split("/")[4]
                if identifier not in apps:
                    raise ConsoleApiError("NOT_FOUND", "missing", status=404)
                if path.endswith("/workflows/draft"):
                    return apps[identifier]["draft"]
                if path.endswith("/api-keys"):
                    return {"data": []}
                return apps[identifier]
            if path.endswith("/credentials"):
                return credentials
            if path.endswith("/add"):
                credentials.append({"id": "credential", "is_default": True, **body})
                return {}
            if "/dynamic-options?" in path:
                return {"options": [{"value": library}]}
            raise AssertionError((method, path))

    monkeypatch.setattr(applications, "DifyConsoleAdmin", Console)
    return connected, parent, source, apps, calls, credentials, lose, approved


def submit(state, **extra):
    fixture, parent = state[0][0], state[1]
    return fixture[2].post(
        "/v1/dify/management/environment-tasks",
        json={
            "connection_task": parent["id"],
            "tenant_id": "same",
            "source_app_id": "source-app",
            "source_version": 7,
            **extra,
        },
    )


def execute(state, identifier):
    fixture = state[0][0]
    owner = "f" * 32
    tasks.worker(fixture[0], fixture[1], {"id": identifier, "owner": owner})
    result = fixture[2].post(
        "/v1/internal/environment-tasks",
        json={"id": identifier, "owner": owner, "prepare": "consumer"},
        headers=build_internal_token_headers(fixture[3].environment_task_key, 300, int(time.time())),
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_consumer_keeps_template_binds_target_and_reconciles_one_disabled_app(consumer_app):
    state = consumer_app
    job = submit(state)
    assert job.status_code == 200, job.text
    assert submit(state).json()["id"] == job.json()["id"]
    original = copy.deepcopy(state[2])
    state[6][0] = True
    assert execute(state, job.json()["id"])["state"] == "reconcile"
    result = execute(state, job.json()["id"])
    assert result["state"] == "observed" and not result["executable"]
    app = next(iter(state[3].values()))
    assert app["enable_site"] is False and app["enable_api"] is False
    params = app["draft"]["graph"]["nodes"][1]["data"]["agent_parameters"]
    assert (
        params["instruction"]
        == original["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]
    )
    assert params["tools"]["value"][0]["settings"]["knowledge_base_ids"]["value"] == [
        state[1]["result"]["knowledge_base_id"]
    ]
    assert state[2] == original and len(state[3]) == 1
    assert state[4].count(("POST", "/console/api/apps/imports")) == 1
    assert not any("publish" in path or "default-credential" in path for _, path in state[4])
    assert "ckx_" not in json.dumps(result)


def test_changed_draft_unknown_zero_and_configuration_preserve_original(consumer_app):
    state = consumer_app
    job = submit(state).json()["id"]
    state[6][0] = True
    assert execute(state, job)["state"] == "reconcile"
    app = next(iter(state[3].values()))
    app["draft"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]["value"] += "编辑修改"
    assert execute(state, job)["state"] == "blocked"
    assert state[4].count(("POST", "/console/api/apps/imports")) == 1


def test_configuration_drift_and_unknown_absence_do_not_create_again(consumer_app):
    state = consumer_app
    job = submit(state).json()["id"]
    state[6][0] = True
    assert execute(state, job)["state"] == "reconcile"
    state[3].clear()
    assert execute(state, job)["state"] == "reconcile"
    Path(state[7]["credential_file"]).write_text('{"api_key":"changed"}')
    assert execute(state, job)["state"] == "blocked"
    assert state[4].count(("POST", "/console/api/apps/imports")) == 1


def test_promoted_adopted_candidate_is_abandoned_and_reminted(consumer_app):
    state = consumer_app
    job = submit(state).json()["id"]
    assert execute(state, job)["state"] == "observed"
    first = next(iter(state[3].values()))
    # 一次越过中央步的发布把候选提升为正式应用（api-enable + 已发布 workflow）；
    # Dify 不提供降级，下一个 prepare 任务经资源继承仍领回这份候选。
    first["enable_api"] = True
    first["workflow"] = {"id": "published"}
    fixture = state[0][0]
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        row = conn.execute("SELECT request,environment FROM environment_tasks WHERE id=?", (job,)).fetchone()
        request = json.loads(row[0])
        request.pop("resources", None)
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,updated_at) VALUES(?,?,?,'queued',?)",
            ("task_" + "1" * 32, row[1], json.dumps(request), time.time()),
        )
    result = execute(state, "task_" + "1" * 32)
    assert result["state"] == "observed"
    assert result["result"]["app_id"] != first["id"]
    assert len(state[3]) == 2
    reminted = state[3][result["result"]["app_id"]]
    assert reminted["enable_api"] is False and "workflow" not in reminted
    assert first["enable_api"] is True and first["workflow"] == {"id": "published"}
    assert state[4].count(("POST", "/console/api/apps/imports")) == 2


def test_unpromoted_adopted_candidate_is_still_reused(consumer_app):
    state = consumer_app
    job = submit(state).json()["id"]
    assert execute(state, job)["state"] == "observed"
    first = next(iter(state[3].values()))
    fixture = state[0][0]
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as conn:
        row = conn.execute("SELECT request,environment FROM environment_tasks WHERE id=?", (job,)).fetchone()
        request = json.loads(row[0])
        request.pop("resources", None)
        conn.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,updated_at) VALUES(?,?,?,'queued',?)",
            ("task_" + "2" * 32, row[1], json.dumps(request), time.time()),
        )
    result = execute(state, "task_" + "2" * 32)
    assert result["state"] == "observed"
    assert result["result"]["app_id"] == first["id"]
    assert len(state[3]) == 1
    assert state[4].count(("POST", "/console/api/apps/imports")) == 1


def test_wrong_target_unapproved_source_and_nonplatform_never_create(consumer_app, monkeypatch):
    for payload in (
        {"tenant_id": "other"},
        {"source_app_id": "other"},
        {"gateway_url": "https://evil.example"},
    ):
        assert submit(consumer_app, **payload).status_code == 403
    monkeypatch.setattr(
        auth, "identity", lambda *_a, **_kw: auth.ManagerIdentity("admin", "platform", False, "synthetic")
    )
    assert submit(consumer_app).status_code == 403
    assert consumer_app[3] == {}


def test_sources_use_published_snapshot_and_require_visible_exact_app(tmp_path, monkeypatch):
    from types import SimpleNamespace

    database = tmp_path / "gateway.db"
    agents = AgentStore(database)
    agent = agents.create(
        "tenant",
        "正式客服",
        "fixture",
        AgentConfig(
            agent_type="customer",
            prompt="正式提示",
            knowledge_base_ids=(),
            model="fixture",
            output_contract="blocks-v1",
        ),
        "fixture",
    )
    snapshot = {
        "name": "正式客服",
        "app_id": "source-app",
        "workspace_id": "ws-source",
        "workflow_id": "workflow-source",
        "workflow": candidate(),
    }
    agents.publish(agent.agent_id, "tenant", agent.revision, snapshot, "fixture")
    route_database = tmp_path / "routes.db"
    routes = ChatRouteBindingStore(route_database)
    routes.publish(
        environment="runtime",
        tenant_id="tenant",
        entry="consumer",
        scenario="consumer_service",
        engine="dify",
        agent_id=agent.agent_id,
        agent_version_no=1,
        app_id="source-app",
        workflow_id="workflow-source",
    )
    workspaces = DifyWorkspaces(database)
    assert workspaces.claim("tenant", "control", owner="fixture", lease_seconds=60)
    workspaces.mark_created("tenant", "control", "ws-source")
    context = SimpleNamespace(
        settings=SimpleNamespace(
            database_file=database,
            chat_route_bindings_file=route_database,
            environment="control",
            execution_environment="runtime",
        ),
        agent_manager=SimpleNamespace(store=agents),
    )
    caller = auth.ManagerIdentity("admin", "platform", True, "synthetic")
    monkeypatch.setattr(auth, "tenants", lambda *_: [{"id": "tenant"}])
    assert consumer.sources(context, caller)["sources"][0]["version_no"] == 1
    selected = consumer.sources(context, caller, "source-app")
    assert selected["workflow"] == snapshot["workflow"]
    for app_id in ("", "other"):
        with pytest.raises(auth.SetupRefused):
            consumer.sources(context, caller, app_id)
    monkeypatch.setattr(auth, "tenants", lambda *_: [])
    assert consumer.sources(context, caller) == {"sources": []}
    with pytest.raises(auth.SetupRefused):
        consumer.sources(context, caller, "source-app")


def test_selected_source_version_change_rejects_before_creating_task(consumer_app):
    assert submit(consumer_app, source_version=6).status_code == 409
    assert submit(consumer_app, source_version=True).status_code == 403
    assert not consumer_app[3]
