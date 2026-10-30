"""对客/内部知识边界（R10）：显式登记才可分发给消费者。

独立验收的反例：本租户的 `internal-only` 库照样通过发布契约 —— 因为判据只有"属于本租户"。
这里断言四条同时成立：属于租户、**已被显式授权对客**、在应用选择内、当前可用；
并且撤销授权之后**已经发布在跑的版本**也不再检索 —— 否则"撤销"只是改了一个下拉项。
"""

import pytest

from aiops_diagnostics.consumer.consumer_flow_contract import publication_settings
from aiops_diagnostics.consumer.consumer_knowledge_grants import (
    AUDIENCE_CUSTOMER,
    AUDIENCE_INTERNAL,
    KnowledgeGrants,
    audience,
)
from aiops_diagnostics.consumer.consumer_knowledge_selection import require_owned
from consumer.test_consumer_flow_contract import BASE, CEILING
from consumer.test_consumer_knowledge_picker import picker_graph


def test_internal_library_of_the_same_tenant_is_not_publishable():
    """本租户 internal-only 库：发布契约必须拒绝。"""
    rows = [{"knowledge_base_id": "internal-only", "name": "内部运营资料", "audience": AUDIENCE_INTERNAL}]
    with pytest.raises(ValueError, match="未授权给客户端使用"):
        publication_settings(picker_graph(["internal-only"]), CEILING, BASE, owned_libraries=rows)


def test_granted_library_publishes_and_records_the_selection():
    rows = [{"knowledge_base_id": "kb-customer", "audience": AUDIENCE_CUSTOMER}]
    result = publication_settings(picker_graph(["kb-customer"]), CEILING, BASE, owned_libraries=rows)
    assert result["knowledge_base_ids"] == ["kb-customer"]


def test_rows_without_the_attribute_are_internal_by_default():
    """缺字段按内部处理（fail closed）：一个没被授权的地方不能靠"字段缺失"变成可分发。"""
    with pytest.raises(ValueError, match="未授权给客户端使用"):
        require_owned(["kb-x"], [{"knowledge_base_id": "kb-x"}])


def test_grant_registry_is_scoped_by_tenant_and_environment(tmp_path):
    db = tmp_path / "gateway.db"
    grants = KnowledgeGrants(db)
    grants.grant("TENANT-A", "env-a", "kb-1", "tester")
    assert grants.granted("TENANT-A", "env-a") == {"kb-1"}
    assert grants.granted("TENANT-B", "env-a") == set()
    assert grants.granted("TENANT-A", "env-b") == set()
    assert grants.revoke("TENANT-A", "env-a", "kb-1") is True
    assert grants.granted("TENANT-A", "env-a") == set()
    # 幂等：重复授予不报错，也不重复
    grants.grant("TENANT-A", "env-a", "kb-1", "tester")
    grants.grant("TENANT-A", "env-a", "kb-1", "tester")
    assert grants.granted("TENANT-A", "env-a") == {"kb-1"}


def test_audience_marks_upstream_rows_instead_of_trusting_them():
    rows = [
        {"knowledge_base_id": "kb-a", "audience": AUDIENCE_CUSTOMER},
        {"knowledge_base_id": "kb-b"},
    ]
    marked = audience(rows, {"kb-b"})
    # 上游自称 customer 但**没有授权行** ⇒ 仍然是内部库
    assert [r["audience"] for r in marked] == [AUDIENCE_INTERNAL, AUDIENCE_CUSTOMER]


def test_revoked_grant_stops_an_already_published_version(tmp_path, monkeypatch):
    """撤销之后**在跑的版本**也不再检索：只在发布时查一次不算隔离。"""
    from types import SimpleNamespace

    from execution.test_app_runtime_dify_entry import dify_chat

    from aiops_diagnostics.consumer import consumer_knowledge_selection as selection
    from aiops_diagnostics.execution.chat_execution import ChatBinding

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        settings = env["settings"]
        grants = KnowledgeGrants(settings.database_file)
        grants.grant("TENANT-A", settings.execution_environment, "kb-own", "fixture")

        class Kb:
            def for_tenant(self, tenant):
                return self

            def list_knowledge_bases(self):
                return [{"knowledge_base_id": "kb-own", "name": "对客库", "document_count": 1}]

        runtime = SimpleNamespace(kb_search_client=Kb(), gateway_settings=settings)
        binding = ChatBinding(
            environment=settings.execution_environment,
            tenant_id="TENANT-A",
            entry="consumer",
            scenario="consumer_service",
            engine="dify",
            app_id="app",
            workflow_id="wf",
            agent_id="agent",
            agent_version_no=1,
            route_revision=1,
            knowledge_base_ids=("kb-own",),
            callback_version=3,
        )
        caller = SimpleNamespace(effective_tenant_id="TENANT-A")
        selection.require_available(runtime, caller, binding)
        grants.revoke("TENANT-A", settings.execution_environment, "kb-own")
        with pytest.raises(Exception, match="知识库不可用|未授权"):
            selection.require_available(runtime, caller, binding)
