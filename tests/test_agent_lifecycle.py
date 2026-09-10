from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_lifecycle import (
    AgentConfig,
    AgentConflict,
    AgentManager,
    AgentNotFound,
    AgentPublishError,
    AgentStore,
    AgentValidationError,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Knowledge:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        self.calls.append((tenant_id, knowledge_base_ids))
        if self.blocked:
            raise AgentPublishError("knowledge base is still parsing")


class _Runtime:
    def shutdown(self) -> None:
        pass


class _Resolver:
    def __init__(self, context: ScopeContext) -> None:
        self.context = context

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, required_scope, third_session
        return self.context


def _context(
    tenant: str = "tenant-a", roles: frozenset[str] = frozenset({"ROLE_AGENT_ADMIN"})
) -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=roles,
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _config(*, agent_type: str = "customer", model: str = "aiops-api") -> AgentConfig:
    return AgentConfig(
        agent_type=agent_type,
        prompt="回答必须引用已授权的业务资料。",
        knowledge_base_ids=("kb-a",),
        model=model,
        output_contract="blocks-v1" if agent_type == "customer" else "diagnosis-v1",
        opening_questions=("怎么处理？",),
        quick_commands=("查看步骤",),
    )


def test_lifecycle_is_tenant_scoped_and_published_snapshot_is_immutable(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_Knowledge())
    admin = _context()

    agent = manager.create(admin, name="客服", description="客服助手", config=_config())
    updated = manager.update(
        admin,
        agent.agent_id,
        expected_revision=agent.revision,
        name="客服 v2",
        description="新的说明",
        config=_config(),
    )
    version = manager.publish(admin, agent.agent_id, expected_revision=updated.revision)
    after_publish = manager.get(admin, agent.agent_id)

    assert version.version_no == 1
    assert version.snapshot["name"] == "客服 v2"
    assert after_publish.status == "published"
    assert manager.version(admin, agent.agent_id, 1).snapshot["prompt"] == _config().prompt
    draft = manager.fork_draft(admin, agent.agent_id, expected_revision=after_publish.revision)
    changed = manager.update(
        admin,
        agent.agent_id,
        expected_revision=draft.revision,
        name="客服 v3",
        description="草稿",
        config=_config(),
    )
    assert manager.version(admin, agent.agent_id, 1).snapshot["name"] == "客服 v2"
    assert manager.publish(admin, agent.agent_id, expected_revision=changed.revision).version_no == 2
    with pytest.raises(AgentConflict):
        manager.update(
            admin,
            agent.agent_id,
            expected_revision=updated.revision,
            name="过期修改",
            description="",
            config=_config(),
        )
    with pytest.raises(AgentNotFound):
        manager.get(_context("tenant-b"), agent.agent_id)


def test_publish_validates_model_and_knowledge_before_writing_version(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "gateway.db")
    blocked_knowledge = _Knowledge(blocked=True)
    manager = AgentManager(store, knowledge_resolver=blocked_knowledge)
    admin = _context()
    agent = manager.create(admin, name="客服", description="", config=_config())

    with pytest.raises(AgentPublishError):
        manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    assert manager.get(admin, agent.agent_id).published_version is None
    assert blocked_knowledge.calls == [("tenant-a", ("kb-a",))]

    bad = manager.create(admin, name="坏模型", description="", config=_config(model="rogue-model"))
    with pytest.raises(AgentValidationError, match="model"):
        manager.publish(admin, bad.agent_id, expected_revision=bad.revision)


def test_disable_preserves_version_and_delete_only_unpublished_draft(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_Knowledge())
    admin = _context()
    draft = manager.create(admin, name="草稿", description="", config=_config())
    manager.delete(admin, draft.agent_id, expected_revision=draft.revision)
    with pytest.raises(AgentNotFound):
        manager.get(admin, draft.agent_id)

    published = manager.create(admin, name="已发布", description="", config=_config())
    manager.publish(admin, published.agent_id, expected_revision=published.revision)
    current = manager.get(admin, published.agent_id)
    disabled = manager.disable(admin, published.agent_id, expected_revision=current.revision)
    assert disabled.status == "disabled"
    assert manager.version(admin, published.agent_id, 1).version_no == 1
    with pytest.raises(AgentConflict):
        manager.delete(admin, published.agent_id, expected_revision=disabled.revision)


def test_agent_management_api_enforces_role_and_returns_opaque_lifecycle_contract(tmp_path: Path) -> None:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    manager = AgentManager(AgentStore(settings.database_file), knowledge_resolver=_Knowledge())
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Resolver(_context()),
        agent_manager=manager,
    )
    payload = {
        "name": "客服",
        "description": "客服助手",
        "agent_type": "customer",
        "prompt": "回答业务问题",
        "knowledge_base_ids": ["kb-a"],
        "model": "aiops-api",
        "output_contract": "blocks-v1",
        "opening_questions": [],
        "quick_commands": [],
    }
    with TestClient(app) as client:
        created = client.post("/v1/agents", headers={"Authorization": "Bearer token"}, json=payload)
        assert created.status_code == 201
        agent = created.json()
        assert agent["agent_id"].startswith("agt_")
        published = client.post(
            f"/v1/agents/{agent['agent_id']}/publish",
            headers={"Authorization": "Bearer token"},
            json={"expected_revision": agent["revision"]},
        )
        assert published.status_code == 200
        assert published.json()["version_no"] == 1
        version = client.get(
            f"/v1/agents/{agent['agent_id']}/versions/1",
            headers={"Authorization": "Bearer token"},
        )
        assert version.status_code == 200
        assert version.json()["snapshot"]["output_contract"] == "blocks-v1"

    viewer_app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Resolver(_context(roles=frozenset({"ROLE_AGENT_VIEWER"}))),
        agent_manager=manager,
    )
    with TestClient(viewer_app) as client:
        denied = client.post("/v1/agents", headers={"Authorization": "Bearer token"}, json=payload)
    assert denied.status_code == 403
