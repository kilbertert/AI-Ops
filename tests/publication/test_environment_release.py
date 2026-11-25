"""受管发布包复用原版本/route事务，恢复不写回历史revision。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from consumer.test_consumer_agent_flow import candidate
from dify.test_dual_entry import dual  # noqa: F401
from operations.test_operator_cpms import operator  # noqa: F401

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.consumer.agent_contract import agent_publication_settings as publication_settings
from aiops_diagnostics.consumer.consumer_publication import frozen_settings
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway.environment_registry import apply
from aiops_diagnostics.publication import environment_release as release


@pytest.fixture
def prepared(dual, tmp_path, monkeypatch):  # noqa: F811
    ctx = dual.context
    ctx.settings.environment = "env-a"
    ctx.agent_manager.allowed_models |= {"synthetic-model"}
    workspace = "workspace-tenant-b"
    operator_app = "aaaaaaaa-1111-2222-3333-111111111111"
    consumer_app = "bbbbbbbb-1111-2222-3333-111111111111"
    store = SelfServiceStore(ctx.settings.database_file)
    store.register(
        app_id=operator_app,
        tenant_id="TENANT-B",
        workspace_id=workspace,
        agent_name="new-operator",
        profile_id="default",
        actor="environment-runtime",
        environment="env-a",
    )
    store.register(
        app_id=consumer_app,
        tenant_id="TENANT-B",
        workspace_id=workspace,
        agent_name="new-consumer",
        profile_id="",
        actor="environment-runtime",
        environment="env-a",
        entry="consumer",
        targets={"knowledge_base_ids": ["kb-own"], "live_order_status": True, "live_billing_record": True},
    )
    directory = tmp_path / "registry/environment.json"
    apply(
        directory,
        [
            {
                "id": "env-a",
                "name": "test",
                "enabled": True,
                "gateway_url": "https://target.example",
                "company_gateway_url": "https://cpms.example",
                "cpms_base_url": "https://cpms.example/cpms",
                "config_ref": str(ctx.settings.server_config_file),
                "tenants": [
                    {"id": "TENANT-A", "name": "old", "enabled": True},
                    {"id": "TENANT-B", "name": "new", "enabled": False},
                ],
            }
        ],
        expected_revision=0,
        actor="fixture",
        managers=[],
    )
    ctx.settings.environment_registry_file = str(directory)
    ctx.settings._environment_registry = None
    key = Path(ctx.settings.chat_dify_key_dir) / (consumer_app + ".key")
    write_private_text(key, "synthetic-app-key")
    flow = json.loads(json.dumps(candidate()).replace("__MODEL__", "aiops-api"))
    ceiling = frozen_settings(
        {
            "tenant_id": "TENANT-B",
            "environment": "env-a",
            "source": "managed",
            "app_id": consumer_app,
            "knowledge_base_ids": ["kb-own"],
            "live_order_status": True,
            "live_billing_record": True,
        }
    )
    frozen = publication_settings(flow, ceiling, [{"knowledge_base_id": "kb-own", "audience": "customer"}])
    manifest = dual.f["route"]
    previous = ctx.agent_manager.store.version(
        manifest.agent_id, "TENANT-A", manifest.agent_version_no
    ).snapshot
    doc = {
        "schema": "aiops-environment-release/1",
        "id": "task_" + "d" * 32,
        "environment": "env-a",
        "tenant_id": "TENANT-B",
        "workspace_id": workspace,
        "actor": "admin",
        "apps": {
            "operator": {
                "app_id": operator_app,
                "manifest": previous["node_manifest"],
                "prompt": "frozen operator prompt",
            },
            "consumer": {
                "app_id": consumer_app,
                "workflow": flow,
                "workflow_id": "cccccccc-1111-2222-3333-111111111111",
                "settings": frozen,
                "key": "synthetic-app-key",
            },
        },
    }
    monkeypatch.setattr(
        release,
        "for_application",
        lambda *a: SimpleNamespace(
            list_knowledge_bases=lambda: [{"knowledge_base_id": "kb-own", "audience": "customer"}]
        ),
    )
    return ctx, doc


def test_frozen_import_route_cas_and_restore_preserve_existing_tenant(prepared):
    ctx, doc = prepared
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    before = [r for r in routes.all() if r.tenant_id == "TENANT-A"]
    first = release.operation(ctx, doc, "prepare")
    assert set(first["versions"]) == {"operator", "consumer"}
    assert release.operation(ctx, doc, "prepare")["versions"] == first["versions"]
    assert routes.get("env-a", "TENANT-B", "consumer", "consumer_service") is None
    published = release.operation(ctx, doc, "publish")
    assert published["routes"] == {"operator": 1, "consumer": 1}
    assert release.operation(ctx, doc, "publish")["routes"] == published["routes"]
    restored = release.operation(ctx, doc, "restore")
    assert restored["phase"] == "restored" and restored["restored_routes"] == {"operator": 2, "consumer": 2}
    assert [r for r in routes.all() if r.tenant_id == "TENANT-A"] == before
    assert not routes.get("env-a", "TENANT-B", "consumer", "consumer_service").active


def test_route_concurrent_change_is_not_overwritten(prepared):
    ctx, doc = prepared
    state = release.operation(ctx, doc, "prepare")
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    v = state["versions"]["consumer"]
    routes.publish(
        environment="env-a",
        tenant_id="TENANT-B",
        entry="consumer",
        scenario="consumer_service",
        engine="dify",
        agent_id=v["agent_id"],
        agent_version_no=v["version_no"],
        app_id="different-app",
        workflow_id=v["workflow_id"],
        workflow_key_ref="different-key",
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="route changed"):
        release.operation(ctx, doc, "publish")
    assert routes.get("env-a", "TENANT-B", "consumer", "consumer_service").app_id == "different-app"


def test_partial_publish_and_partial_restore_reconcile_without_repeating_cas(prepared, monkeypatch):
    ctx, doc = prepared
    release.operation(ctx, doc, "prepare")
    advance = release.advance

    def fail_consumer(context, document, versions, expected):
        if "consumer" in versions:
            raise ValueError("simulated second endpoint unavailable")
        return advance(context, document, versions, expected)

    monkeypatch.setattr(release, "advance", fail_consumer)
    with pytest.raises(ValueError, match="second endpoint"):
        release.operation(ctx, doc, "publish")
    assert release.read_record(ctx.settings, doc["id"])["routes"] == {"operator": 1}
    monkeypatch.setattr(release, "advance", advance)
    published = release.operation(ctx, doc, "publish")
    assert published["routes"] == {"operator": 1, "consumer": 1}
    restore = release.restore

    def fail_restore(context, document, previous, expected):
        if "consumer" in previous:
            raise ValueError("simulated restore unavailable")
        return restore(context, document, previous, expected)

    monkeypatch.setattr(release, "restore", fail_restore)
    with pytest.raises(ValueError, match="restore unavailable"):
        release.operation(ctx, doc, "restore")
    assert release.read_record(ctx.settings, doc["id"])["restored_routes"] == {"operator": 2}
    monkeypatch.setattr(release, "restore", restore)
    recovered = release.operation(ctx, doc, "restore")
    assert recovered["restored_routes"] == {"operator": 2, "consumer": 2}
    assert release.operation(ctx, doc, "restore") == recovered


def test_same_content_later_revision_is_not_owned_by_this_operation(prepared):
    ctx, doc = prepared
    release.operation(ctx, doc, "prepare")
    release.operation(ctx, doc, "publish")
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    row = routes.get("env-a", "TENANT-B", "operator", "operator_order_diagnosis")
    from dataclasses import asdict

    other = asdict(row)
    for key in ("state", "revision", "updated_at"):
        other.pop(key)
    routes.publish(**other, expected_revision=1)
    with pytest.raises(ValueError, match="route changed"):
        release.operation(ctx, doc, "publish")
    with pytest.raises(ValueError, match="route changed"):
        release.operation(ctx, doc, "restore")
    assert routes.get("env-a", "TENANT-B", "operator", "operator_order_diagnosis").revision == 2


@pytest.mark.parametrize(
    "field,value",
    [("workflow_id", "different-workflow"), ("workflow_key_ref", "/other.key"), ("engine", "node")],
)
def test_foreign_route_with_same_version_is_not_adopted_or_restored(prepared, field, value):
    ctx, doc = prepared
    state = release.operation(ctx, doc, "prepare")
    routes = ChatRouteBindingStore(ctx.settings.chat_route_bindings_file)
    wanted = dict(state["versions"]["consumer"]["binding"])
    wanted.pop("state")
    wanted[field] = value
    foreign = routes.publish(**wanted, expected_revision=0)
    with pytest.raises(ValueError, match="route changed"):
        release.operation(ctx, doc, "publish")
    with pytest.raises(ValueError, match="route changed"):
        release.operation(ctx, doc, "restore")
    assert routes.get(doc["environment"], doc["tenant_id"], "consumer", "consumer_service") == foreign
