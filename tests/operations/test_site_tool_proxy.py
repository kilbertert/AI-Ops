"""站点工具走真实 Gateway 的授权、取数与证据回调；仅公司身份和 CPMS 合成。"""

import json
from dataclasses import replace

import pytest

from aiops_diagnostics.identity.scope_context import DataScope
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import (  # noqa: F401
    HEADERS,
    NODE_A_TOKEN,
    operator,
    scope,
    tenant_owner,
    terminal,
)


@pytest.mark.parametrize("arguments", [{}, {"name": "天河"}, {"name": "站点"}, {"address": "站点"}])
@pytest.mark.parametrize("mode", ["scoped", "tenant", "empty", "wrong-site", "wrong-tenant"])
def test_site_tool_proxy_preserves_cpms_page_and_accepts_evidence(operator, monkeypatch, arguments, mode):  # noqa: F811
    f = operator
    if mode == "tenant":
        f["resolver"].current = tenant_owner()
    elif mode == "empty":
        f["resolver"].current = replace(
            scope(), data_scope=DataScope(type="organ", site_ids=()), scope_fingerprint=""
        )
    elif mode == "wrong-site":
        f["state"]["site"] = "SITE-X"
    elif mode == "wrong-tenant":
        f["state"]["tenant"] = "TENANT-X"
    if mode.startswith("wrong-"):
        f["state"]["site_ignore_filter"] = True
    replies = []
    failures = []

    def dispatch(_, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        published = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        replies.append(published)
        assert published.status_code == 200, published.text
        headers.update(
            {
                "X-AI-Ops-Exec-Config": published.json()["sha256"],
                "X-AI-Ops-Tool-Contract": operator_cpms.TOOL_CONTRACT,
            }
        )
        path = f"/v1/internal/operator-runs/{run_id}/tools/cpms_site_search"
        grant = f["client"].post(path + "/authorization", headers=headers, json={"arguments": arguments})
        replies.append(grant)
        assert grant.status_code == 200, grant.text
        grant = grant.json()
        target = grant["request"]
        response = f["client"].get(
            grant["base_url"] + target["path"] + "?" + target["query"],
            headers={"Authorization": "Bearer " + grant["bearer"]},
        )
        replies.append(response)
        if mode.startswith("wrong-"):
            assert response.status_code == 403, response.text
            assert not f["ctx"].operator_cpms.tasks[run_id].evidence
            return
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        assert page["current"] == 1 and page["size"] == 50
        assert page["records"] == (
            []
            if mode == "empty" or "站点" in arguments.values()
            else [
                {
                    "id": "SITE-1",
                    "tenantId": "TENANT-A",
                    "name": "天河测试站",
                    "address": "广州市天河区测试路",
                }
            ]
        )
        assert "PRIVATE" not in response.text
        evidence = dict(f["ctx"].operator_cpms.tasks[run_id].evidence["cpms_site_search"])
        evidence.pop("response")
        recorded = f["client"].post(
            path + "/evidence",
            headers=headers,
            json={"authorization_id": grant["authorization_id"], "evidence": evidence},
        )
        assert recorded.status_code == 200, recorded.text
        answer = {
            "kind": "site_search",
            "text": "查询结果如下。",
            "name": arguments.get("name", ""),
            "address_terms": [arguments["address"]] if arguments.get("address") else [],
        }
        completed = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={"status": "completed", "answer": json.dumps(answer)},
        )
        assert completed.status_code == 200, completed.text

    original_dispatch = dispatch

    def dispatch(*args):
        try:
            return original_dispatch(*args)
        except Exception as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": "查询天河站点"})
    assert response.status_code == 202, response.text
    result = terminal(f, response.json()["qa_id"])
    assert not failures, failures
    assert replies, result.text
    if mode.startswith("wrong-"):
        assert result.json()["status"] == "failed"
        return
    assert all(r.status_code == 200 for r in replies), [(r.status_code, r.text) for r in replies]
    assert result.json()["status"] == "completed", result.text
    if mode == "empty":
        assert not f["state"]["calls"]
    elif "站点" in arguments.values():
        assert "天河测试站" not in result.text
    else:
        assert "天河测试站" in result.text
