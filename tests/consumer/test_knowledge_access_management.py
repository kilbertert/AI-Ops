"""客户从应用管理面授权自己的库；请求不能指定其他租户。"""

from dify.test_dual_entry import dual, request, run  # noqa: F401
from operations.test_operator_cpms import operator  # noqa: F401

from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
from aiops_diagnostics.dify import self_service_identity as auth


def test_customer_can_enable_and_revoke_own_library_only(dual, monkeypatch):  # noqa: F811
    result = run(dual, request(dual))
    app = result["entries"]["consumer"]["app_id"]
    tenant = "TENANT-B"
    monkeypatch.setattr(auth, "identity", lambda *a: auth.ManagerIdentity("manager", tenant, False, "token"))
    rows = [{"knowledge_base_id": "own-library", "name": "客户资料"}]
    monkeypatch.setattr(dual.kb, "list_knowledge_bases", lambda: rows)
    client = dual.f["client"]
    dual.settings.dify_publish_api_key = "publish-secret"
    headers = {"Authorization": "Bearer publish-secret", "X-AIOps-Console-Session": "normal-session"}
    path = "/v1/dify/management/knowledge"
    response = client.post(path, headers=headers, json={"app_id": app})
    assert response.status_code == 200, response.text
    assert response.json()["libraries"] == [{"id": "own-library", "name": "客户资料", "allowed": False}]
    for allowed in (True, False):
        response = client.post(
            path + "/access",
            headers=headers,
            json={"app_id": app, "knowledge_base_id": "own-library", "allowed": allowed},
        )
        assert response.status_code == 200, response.text
        assert response.json()["allowed"] is allowed
        assert (
            "own-library" in KnowledgeGrants(dual.settings.database_file).granted(tenant, "env-a")
        ) is allowed
    bad = client.post(
        path + "/access",
        headers=headers,
        json={"app_id": app, "knowledge_base_id": "someone-else", "allowed": True},
    )
    assert bad.status_code == 403
    bad = client.post(
        path + "/access",
        headers=headers,
        json={"app_id": app, "knowledge_base_id": "own-library", "allowed": True, "tenant_id": "TENANT-A"},
    )
    assert bad.status_code == 422
    monkeypatch.setattr(
        auth, "identity", lambda *a: auth.ManagerIdentity("other", "TENANT-C", False, "token")
    )
    assert client.post(path, headers=headers, json={"app_id": app}).status_code == 403
