from pathlib import Path

from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.publication.qa_rag import select_customer_agent


class _PublishOkResolver:
    """Knowledge binding resolver that accepts publishes (test fixture)."""

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _publish_ok() -> _PublishOkResolver:
    return _PublishOkResolver()


def _admin_context():
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def test_select_customer_agent_requires_published_customer(tmp_path: Path) -> None:
    """Only a published customer agent with KBs serves the RAG path."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    agent = manager.create(admin, name="客服", description="", config=_customer_config())
    assert select_customer_agent(store, "tenant-a") is None  # draft — not served
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None
    assert selection.agent_id == agent.agent_id
    assert selection.knowledge_base_ids == ("kb-a",)
    assert selection.version_no == 1
    # Another tenant sees nothing (tenant scope, no existence leak).
    assert select_customer_agent(store, "tenant-b") is None


def test_select_customer_agent_skips_promo_pinned_agent(tmp_path: Path) -> None:
    """A promotional agent pinned by a published shortcut never serves the
    plain customer QA path (41 live regression, 2026-09-15): creating a newer
    promotional agent made `select_customer_agent` hand the customer-service
    questions to the promo agent because list() orders by created_at DESC."""
    from aiops_diagnostics.publication.shortcut_lifecycle import ShortcutManager, ShortcutStore

    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    service = manager.create(admin, name="客服", description="", config=_customer_config())
    manager.publish(admin, service.agent_id, expected_revision=service.revision)
    promo = manager.create(admin, name="宣传", description="", config=_customer_config())
    manager.publish(admin, promo.agent_id, expected_revision=promo.revision)

    # Without a shortcut pin the newest (promo) agent still serves — old behavior.
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None and selection.agent_id == promo.agent_id

    # Pin the promo agent via a published shortcut: customer QA returns to the
    # customer-service agent, and the promo agent serves only its promo route.
    shortcut_manager = ShortcutManager(ShortcutStore(store.path))
    shortcut = shortcut_manager.create(
        admin,
        {
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例"},
            "descriptions": {"zh": "说明"},
            "question_templates": {"zh": "我想看看客户案例"},
            "target_agent_version": f"{promo.agent_id}#v1",
        },
    )
    shortcut_manager.publish(admin, shortcut.shortcut_id, expected_revision=shortcut.revision)
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None and selection.agent_id == service.agent_id


def test_select_customer_agent_skips_operations_type(tmp_path: Path) -> None:
    """A published operations agent never serves the customer QA path."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    ops = manager.create(
        admin,
        name="运维",
        description="",
        config=AgentConfig(
            agent_type="operations",
            prompt="只读诊断。",
            knowledge_base_ids=("kb-a",),
            model="aiops-api",
            output_contract="diagnosis-v1",
        ),
    )
    manager.publish(admin, ops.agent_id, expected_revision=ops.revision)
    assert select_customer_agent(store, "tenant-a") is None


def test_disabled_agent_is_not_selected(tmp_path: Path) -> None:
    """A disabled published agent stops serving new questions."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    agent = manager.create(admin, name="客服", description="", config=_customer_config())
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    published = manager.get(admin, agent.agent_id)
    manager.disable(admin, agent.agent_id, expected_revision=published.revision)
    assert select_customer_agent(store, "tenant-a") is None


def _customer_config() -> AgentConfig:
    return AgentConfig(
        agent_type="customer",
        prompt="回答必须引用已授权的业务资料。",
        knowledge_base_ids=("kb-a",),
        model="aiops-api",
        output_contract="blocks-v1",
    )
