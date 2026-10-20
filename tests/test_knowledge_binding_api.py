"""知识库挂接从"工程师填表"变成"运营点选"（ADR 0048）。

PRD 的一句话：**在前端点击就能完成知识库配置**。旧路径要求填
`knowledge_id:tenant:kb1|kb2` 这一行 env，还要知道自己的 RAGFlow 账号映射到哪个
tenant —— 那是数据录入，不是业务决策，而它只有工程师会做。

这些测试钉住三条：可选列表只回本租户的库、挂接落库且立刻生效、以及"别人的/不存在的"
在响应上不可区分。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TENANT = "tenant-a"
OTHER = "tenant-b"
KB_A = "8701c742b28111f18637d95f7710e3a3"
KB_B = "4f4bc674ad8911f1ae704bfc8c544ea6"


class _FakeKbClient:
    """The kb-service client surface these routes use, scripted per tenant."""

    def __init__(self, per_tenant: dict[str, list[dict]]) -> None:
        self.per_tenant = per_tenant
        self._tenant = "aiops"
        self.asked: list[str] = []

    def for_tenant(self, tenant_id: str) -> _FakeKbClient:
        clone = _FakeKbClient(self.per_tenant)
        clone._tenant = tenant_id
        clone.asked = self.asked
        return clone

    def list_knowledge_bases(self) -> list[dict]:
        self.asked.append(self._tenant)
        return list(self.per_tenant.get(self._tenant, []))


class _Caller:
    """A caller resolver that accepts one bearer token per role."""

    def resolve(self, authorization, **_kwargs):
        del authorization
        from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

        subject = SubjectRecord(b_user_id="B-1", tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({"aiops:agents:manage"}),
        )


class _Runtime:
    """Just the two attributes these routes read."""

    def __init__(self, kb: _FakeKbClient) -> None:
        self.kb_search_client = kb
        self.media_signer = None

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path, per_tenant: dict[str, list[dict]]):
    from fastapi.testclient import TestClient

    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge_retrieval import MediaResourceSigner

    os.chmod(tmp_path, 0o750)
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)
    store = GatewayStore(settings.database_file)
    kb = _FakeKbClient(per_tenant)
    runtime = _Runtime(kb)
    runtime.media_signer = MediaResourceSigner("secret", ttl_seconds=600)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Caller(),  # type: ignore[arg-type]
    )
    return TestClient(app), store, kb


def test_available_lists_only_this_tenants_datasets(tmp_path: Path) -> None:
    """The picker shows what the caller's tenant owns — nothing else.

    Asking another tenant's name here would make "which do I tick" ambiguous at
    best and a cross-tenant grant at worst, so the tenant header is the caller's
    own, not something the request can choose.
    """
    client, _store, kb = _client(
        tmp_path,
        {
            TENANT: [{"knowledge_base_id": KB_A, "name": "41-客服知识库"}],
            OTHER: [{"knowledge_base_id": KB_B, "name": "别人的库"}],
        },
    )
    response = client.get("/v1/knowledge/available", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == TENANT
    assert [item["knowledge_base_id"] for item in body["knowledge_bases"]] == [KB_A]
    assert body["knowledge_bases"][0]["bound"] is False
    assert kb.asked == [TENANT], "the listing must be scoped to the caller's tenant"


def test_put_then_list_then_delete_round_trips(tmp_path: Path) -> None:
    """The click writes; the retrieval route reads it live; delete removes it."""
    client, store, _kb = _client(tmp_path, {TENANT: []})
    headers = {"Authorization": "Bearer t"}
    created = client.put(
        "/v1/knowledge/sources/kb-canary-2",
        headers=headers,
        json={
            "knowledge_id": "kb-canary-2",
            "tenant_id": TENANT,
            "knowledge_base_ids": [KB_A],
            "display_name": "41-客服知识库",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["knowledge_base_ids"] == [KB_A]

    # Written where the retrieval route reads it, not just echoed back.
    rows = store.list_knowledge_id_bindings()
    assert [(r["knowledge_id"], r["tenant_id"], r["knowledge_base_ids"]) for r in rows] == [
        ("kb-canary-2", TENANT, (KB_A,))
    ]

    listed = client.get("/v1/knowledge/sources", headers=headers).json()
    assert [item["knowledge_id"] for item in listed["sources"]] == ["kb-canary-2"]

    removed = client.delete("/v1/knowledge/sources/kb-canary-2", headers=headers)
    assert removed.status_code == 200
    assert store.list_knowledge_id_bindings() == []


def test_put_replaces_rather_than_merges(tmp_path: Path) -> None:
    """Unticking a library has to be expressible: the submitted list IS the state."""
    client, store, _kb = _client(tmp_path, {TENANT: []})
    headers = {"Authorization": "Bearer t"}
    body = {"knowledge_id": "kb-x", "tenant_id": TENANT, "knowledge_base_ids": [KB_A, KB_B]}
    client.put("/v1/knowledge/sources/kb-x", headers=headers, json=body)
    body["knowledge_base_ids"] = [KB_A]
    client.put("/v1/knowledge/sources/kb-x", headers=headers, json=body)

    assert store.list_knowledge_id_bindings()[0]["knowledge_base_ids"] == (KB_A,)


def test_a_tenant_mismatch_is_refused_not_corrected(tmp_path: Path) -> None:
    """A silent rewrite would leave the page and the database disagreeing."""
    client, store, _kb = _client(tmp_path, {TENANT: []})
    response = client.put(
        "/v1/knowledge/sources/kb-x",
        headers={"Authorization": "Bearer t"},
        json={"knowledge_id": "kb-x", "tenant_id": OTHER, "knowledge_base_ids": [KB_A]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "KNOWLEDGE_TENANT_MISMATCH"
    assert store.list_knowledge_id_bindings() == []


def test_another_tenants_source_is_indistinguishable_from_absent(tmp_path: Path) -> None:
    """Same 404 for "never existed" and "not yours": no existence disclosure."""
    client, store, _kb = _client(tmp_path, {TENANT: []})
    store.set_knowledge_id_binding("kb-theirs", OTHER, (KB_B,))

    response = client.delete("/v1/knowledge/sources/kb-theirs", headers={"Authorization": "Bearer t"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KNOWLEDGE_SOURCE_NOT_FOUND"
    assert store.get_knowledge_id_binding("kb-theirs") is not None, "it must be left alone"


def test_available_marks_already_bound_datasets(tmp_path: Path) -> None:
    """One request is enough for the page to render ticks."""
    client, store, _kb = _client(tmp_path, {TENANT: [{"knowledge_base_id": KB_A, "name": "库A"}]})
    store.set_knowledge_id_binding("kb-x", TENANT, (KB_A,))

    body = client.get("/v1/knowledge/available", headers={"Authorization": "Bearer t"}).json()
    assert body["knowledge_bases"][0]["bound"] is True


@pytest.mark.parametrize("bad", ["has space", "a" * 200])
def test_invalid_knowledge_id_is_rejected_by_the_schema(tmp_path: Path, bad: str) -> None:
    client, _store, _kb = _client(tmp_path, {TENANT: []})
    response = client.put(
        "/v1/knowledge/sources/x",
        headers={"Authorization": "Bearer t"},
        json={"knowledge_id": bad, "tenant_id": TENANT, "knowledge_base_ids": [KB_A]},
    )
    assert response.status_code == 422
