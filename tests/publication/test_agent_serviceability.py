"""`agent_serviceability` —— 「还有没有版本在服务」的**唯一判据**（#922）。

判据表就是这两个函数。这里钉的是**两个方向相反的错误**同时被修掉：

- 停用之后仍被当成在服务（安全侧）；
- 编辑草稿就被当成下线（可用性侧）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aiops_diagnostics.publication.agent_serviceability import LIVE_STATUSES, live_version, serves


@dataclass(frozen=True)
class _Agent:
    status: str
    published_version: int | None


@pytest.mark.parametrize(
    ("status", "version", "expected"),
    [
        ("published", 1, 1),  # 正常服务
        ("draft", 1, 1),  # 编辑中：线上仍是那一版
        ("draft", None, None),  # 从未发布过
        ("disabled", 1, None),  # 停用：立刻停服，哪怕指针还在
        ("published", None, None),  # 指针被清掉（数据异常）也不算在服务
    ],
)
def test_live_version_is_the_published_pointer(
    status: str, version: int | None, expected: int | None
) -> None:
    assert live_version(_Agent(status, version)) == expected


def test_only_disabled_is_excluded_from_live_statuses() -> None:
    """状态词表来自 `agent_lifecycle.AGENT_STATUSES`；这里只钉「谁不算在服务」。

    新增一个状态时这条会红，提醒回答「它在不在服务」而不是让它默默落进某个分支。
    """
    assert frozenset({"published", "draft"}) == LIVE_STATUSES


def test_serves_is_equality_with_the_live_version() -> None:
    live = _Agent("draft", 3)
    assert serves(live, 3)
    assert not serves(live, 2)  # 更旧的版本不服务
    assert not serves(_Agent("disabled", 3), 3)


class _PublishOkResolver:
    """Knowledge binding resolver that accepts publishes (同 test_qa_rag 的 fixture)。"""

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def test_a_draft_agent_can_still_be_disabled(tmp_path) -> None:
    """草稿态的 agent 必须还能被停用 —— 否则它无法下线。

    改前 `disable()` 的 UPDATE 限定 `status = 'published'`；那时草稿本来就不服务，
    所以这个缺口没有后果。判据改为读 `published_version` 之后，草稿仍在服务，
    这条 UPDATE 就成了唯一的下线通道 —— 不放开它，运营只能看着它一直服务。
    """
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
    from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentManager, AgentStore

    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_PublishOkResolver())
    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    admin = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset(),
    )
    agent = manager.create(
        admin,
        name="客服",
        description="",
        config=AgentConfig(
            agent_type="customer",
            prompt="p",
            knowledge_base_ids=("kb-a",),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    manager.fork_draft(admin, agent.agent_id, expected_revision=manager.get(admin, agent.agent_id).revision)
    disabled = manager.disable(
        admin, agent.agent_id, expected_revision=manager.get(admin, agent.agent_id).revision
    )
    assert disabled.status == "disabled"
    assert live_version(disabled) is None
