"""D28：候选应用停用导入与复合任务的恢复判据。"""

import json
import time
from pathlib import Path
from uuid import UUID, uuid5

import pytest
import yaml
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401

from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.dify import environment_operator as operator
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError
from aiops_diagnostics.gateway import environment_tasks as tasks
from gateway.test_candidate_knowledge_connection import (  # noqa: F401
    connected,
    prepare_connection,
    request_job,
)
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import prepared  # noqa: F401


@pytest.fixture
def application(connected, tmp_path, monkeypatch):  # noqa: F811
    fixture, _, settings, *_ = connected
    connection = prepare_connection(connected, request_job(connected))
    assert connection["state"] == "observed"
    secret = tmp_path / "model.json"
    secret.write_text('{"api_key":"synthetic"}')
    secret.chmod(0o600)
    settings.operator_profiles_file = "fixture-profile"
    template = dict(
        plugin="fixture",
        provider="fixture",
        name="fixture",
        credential_file=str(secret),
        completion_params={"max_tokens": 4096},
    )
    monkeypatch.setattr(operator, "model", lambda _: template)
    apps, calls, lose = {}, [], [False]

    class Console:
        def __init__(self, _):
            pass

        def _call(self, ws, path, method="GET", body=None, **kwargs):
            assert ws == connection["result"]["workspace_id"]
            calls.append((method, path))
            if "/datasets/external-knowledge-api/" in path:
                return connected[3][0]
            if path == "/console/api/apps/imports":
                if method == "GET":
                    return {"aiops_idempotent_import": 1, "aiops_candidate_import": 1}
                doc = yaml.safe_load(body["yaml_content"])
                assert doc["aiops_candidate"] is True
                identifier = str(
                    uuid5(UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"), ws + ":" + body["request_id"])
                )
                apps[identifier] = {
                    "id": identifier,
                    "name": body["name"],
                    "mode": "agent-chat",
                    "enable_api": False,
                    "enable_site": False,
                    "model_config": doc["model_config"],
                }
                if lose[0]:
                    raise ConsoleApiError("DIFY_CONSOLE_UNREACHABLE", "lost")
                return {"app_id": identifier, "status": "completed"}
            if path.startswith("/console/api/apps/"):
                if path.rsplit("/", 1)[-1] not in apps:
                    raise ConsoleApiError("NOT_FOUND", "missing", status=404)
                return apps[path.rsplit("/", 1)[-1]]
            if "/tool-provider/api/get" in path:
                return {
                    "schema": json.dumps({"servers": [{"url": "https://a.example.test"}]}),
                    "credentials": {"auth_type": "none"},
                }
            return [{"name": "cpms-node", "id": "provider-fixture"}]

    monkeypatch.setattr(operator, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(operator.self_service_prepare, "model_credentials", lambda *args: None)

    def provider(_, ws, base, **kwargs):
        assert base == "https://a.example.test" and ws == connection["result"]["workspace_id"]

    monkeypatch.setattr(operator, "ensure_node_provider", provider)
    yield connected, connection, apps, calls, lose


def submit(application):
    source, connection, *_ = application
    response = source[0][2].post(
        "/v1/dify/management/environment-tasks",
        json={"connection_task": connection["id"], "tenant_id": "same"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute(application, job):
    fixture = application[0][0]
    owner = "e" * 32
    tasks.worker(fixture[0], fixture[1], {"id": job["id"], "owner": owner})
    result = fixture[2].post(
        "/v1/internal/environment-tasks",
        json={"id": job["id"], "owner": owner, "prepare": "operator"},
        headers=build_internal_token_headers(fixture[3].environment_task_key, 300, int(time.time())),
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_candidate_app_stays_disabled_and_reuses_import_after_response_loss(application):
    _, _, apps, calls, lose = application
    job = submit(application)
    assert submit(application)["id"] == job["id"]
    lose[0] = True
    assert execute(application, job)["state"] == "reconcile"
    result = execute(application, job)
    assert result["state"] == "observed" and result["executable"] is False
    assert len(apps) == 1 and not next(iter(apps.values()))["enable_api"]
    assert calls.count(("POST", "/console/api/apps/imports")) == 1
    assert result["result"]["checks"][-1] == {"id": "business", "state": "pending"}


def test_unknown_zero_and_cancel_never_recreate_or_publish(application):
    fixture = application[0][0]
    _, _, apps, calls, lose = application
    job = submit(application)
    lose[0] = True
    assert execute(application, job)["state"] == "reconcile"
    created = dict(apps)
    apps.clear()
    assert execute(application, job)["state"] == "reconcile"
    tasks.inspect(
        fixture[0], ("control", "platform", "admin"), identifier=job["id"], reference=fixture[1], cancel=True
    )
    apps.update(created)
    assert execute(application, job)["state"] == "cancelled"
    assert calls.count(("POST", "/console/api/apps/imports")) == 1
    assert not any("publish" in path for _, path in calls)


def test_cancelled_parent_and_changed_model_never_create_application(application):
    fixture = application[0][0]
    connection = application[1]
    job = submit(application)
    settings = fixture[3]
    template = operator.model(settings)
    Path(template["credential_file"]).write_text('{"api_key":"changed"}')
    assert execute(application, job)["state"] == "blocked"
    assert not application[2]
    tasks.inspect(
        fixture[0],
        ("control", "platform", "admin"),
        identifier=connection["id"],
        reference=fixture[1],
        cancel=True,
    )
    response = fixture[2].post(
        "/v1/dify/management/environment-tasks",
        json={"connection_task": connection["id"], "tenant_id": "same"},
    )
    assert response.status_code == 403
