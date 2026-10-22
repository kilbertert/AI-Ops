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
    """调用者解析器。

    **默认刻意不给任何平台角色**（``roles`` 为空）：判据已改成"本租户内的调用者"，
    而真实公司账号（Ulink 运营：``ROLE_ADMIN`` + ``CPO``）确实一个 AI-Ops 角色都没有。
    若这里继续照抄 ``ROLE_AGENT_ADMIN``，测试就会在一条真实身份永远走不到的分支上全绿 ——
    而那正是 2026-10-22 真机 403 之前的形状。
    """

    def __init__(self, tenant: str = TENANT) -> None:
        self.tenant = tenant

    def resolve(self, authorization, **_kwargs):
        del authorization
        from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

        subject = SubjectRecord(b_user_id="B-1", tenant_id=self.tenant)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=self.tenant,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:agents:manage"}),
        )


class _Runtime:
    """Just the two attributes these routes read."""

    def __init__(self, kb: _FakeKbClient) -> None:
        self.kb_search_client = kb
        self.media_signer = None

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path, per_tenant: dict[str, list[dict]], *, caller=None):
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
        caller_resolver=caller or _Caller(),  # type: ignore[arg-type]
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


def test_the_page_is_reachable_under_v1(tmp_path: Path) -> None:
    """The page must live under `/v1/`, which is all the public nginx proxies.

    A top-level `/knowledge` is a 404 from outside (measured) — the page would
    exist and be unreachable, which is worse than not existing.
    """
    client, _store, _kb = _client(tmp_path, {TENANT: []})
    response = client.get("/v1/knowledge")
    assert response.status_code == 200
    assert "Knowledge ID" in response.text
    # It must call the same-origin API with the operator entry header, because
    # only that branch of the public nginx map preserves the caller's token.
    assert '"/v1/knowledge/available"' in response.text
    assert '"X-Business-Entry": "operator"' in response.text


# ── 判据：本租户内的调用者，不要求平台级全局码（2026-10-22） ───────────────
#
# 真机事实：Ulink 运营（ROLE_ADMIN + CPO，两个角色都是租户级）调
# /v1/knowledge/available 得 403，因为判据要的是 aiops:agent:edit。那个码建于
# ADR-0029 的「平台级单工作区」时代，1:1:1 之后跨租户越权结构上已不存在。


def test_a_tenant_admin_without_any_platform_role_can_read_the_picker(tmp_path: Path) -> None:
    """没有 AI-Ops 角色、只有本租户身份 ⇒ 放行。

    这正是 Ulink 运营的形状。若这条红，说明判据又退回成"必须有平台全局码"。
    """
    client, _store, _kb = _client(tmp_path, {TENANT: []})

    response = client.get("/v1/knowledge/available", headers={"Authorization": "Bearer t"})

    assert response.status_code == 200, response.text
    assert response.json()["tenant_id"] == TENANT


def test_a_tenant_admin_without_any_platform_role_can_bind(tmp_path: Path) -> None:
    client, _store, _kb = _client(tmp_path, {TENANT: [{"knowledge_base_id": "kb-1", "name": "kb"}]})

    response = client.put(
        "/v1/knowledge/sources/kb-mine",
        headers={"Authorization": "Bearer t"},
        json={
            "knowledge_id": "kb-mine",
            "tenant_id": TENANT,
            "knowledge_base_ids": ["kb-1"],
            "display_name": "",
        },
    )

    assert response.status_code == 200, response.text


def test_a_caller_without_a_tenant_is_still_refused(tmp_path: Path) -> None:
    """门没有消失：没有租户身份的调用者仍然进不来。

    放宽的是"要不要平台全局码"，不是"要不要身份"。
    """
    client, _store, _kb = _client(tmp_path, {TENANT: []}, caller=_Caller(tenant=""))

    response = client.get("/v1/knowledge/available", headers={"Authorization": "Bearer t"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "KNOWLEDGE_FORBIDDEN"


def test_one_tenant_cannot_bind_against_another(tmp_path: Path) -> None:
    """判据改成租户级之后，跨租户这条边界必须仍由 tenant_matches 挡住。"""
    client, _store, _kb = _client(tmp_path, {TENANT: [{"knowledge_base_id": "kb-1", "name": "kb"}]})

    response = client.put(
        "/v1/knowledge/sources/kb-theirs",
        headers={"Authorization": "Bearer t"},
        json={
            "knowledge_id": "kb-theirs",
            "tenant_id": "9999999999999999999",
            "knowledge_base_ids": ["kb-1"],
            "display_name": "",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "KNOWLEDGE_TENANT_MISMATCH"


def test_one_tenant_cannot_overwrite_another_tenants_source(tmp_path: Path) -> None:
    """B 不能改 A 已经登记的知识源（2026-10-22 负向真机暴露的洞）。

    旧判据只比较「请求体的 tenant 与调用者」，从不问这个 `knowledge_id` 现在归谁 ⇒
    B 把 body 的 tenant 填成自己，就能覆盖 A 的行。危害是**破坏**：A 的知识源指向
    B 的库、或名字被改，A 的运营下次进 Dify 会 404。

    与 DELETE 同形拒绝（404，不区分"不存在"与"是别人的"）。
    """
    client, store, _kb = _client(
        tmp_path,
        {TENANT: [{"knowledge_base_id": "kb-1", "name": "kb"}]},
    )
    store.set_knowledge_id_binding("kb-shared", OTHER, ("kb-other",), display_name="别人的")

    response = client.put(
        "/v1/knowledge/sources/kb-shared",
        headers={"Authorization": "Bearer t"},
        json={
            "knowledge_id": "kb-shared",
            "tenant_id": TENANT,  # 调用者自己的租户 —— 旧判据会放行
            "knowledge_base_ids": ["kb-1"],
            "display_name": "hijack",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "KNOWLEDGE_SOURCE_NOT_FOUND"
    # 原行一个字节都没变
    row = store.get_knowledge_id_binding("kb-shared")
    assert row["tenant_id"] == OTHER
    assert row["display_name"] == "别人的"
