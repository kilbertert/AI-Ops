"""真实工具授权/代理/证据/回读，CPMS严格执行其子串过滤契约。"""

from urllib.parse import parse_qs, urlsplit

import pytest

from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_conversation import drive_site_tool
from operations.test_operator_cpms import HEADERS, NODE_A_TOKEN, environment, terminal


@pytest.mark.parametrize("terms", [["广州", "天河"], ["天河"]])
def test_compound_place_uses_site_search_in_real_tool_path(tmp_path, monkeypatch, terms):
    with environment(tmp_path, monkeypatch) as f:
        calls = []
        row = {
            "id": "SITE-1",
            "tenantId": "TENANT-A",
            "name": "HuaJing Station",
            "address": "天河公园 Tian He Qu, Guang Zhou Shi",
        }
        original = operator_cpms.cpms_get

        def cpms(url, bearer, tenant, **kwargs):
            if urlsplit(url).path != "/chSite/page":
                return original(url, bearer, tenant, **kwargs)
            assert bearer == "operator-private" and tenant == "TENANT-A"
            params = parse_qs(urlsplit(url).query)
            calls.append(params)
            address = params.get("address", [""])[0]
            records = [row] if not address or address in row["address"] else []
            return {
                "code": 0,
                "data": {
                    "records": records,
                    "total": len(records),
                    "current": int(params["current"][0]),
                    "size": 50,
                },
            }

        monkeypatch.setattr(operator_cpms, "cpms_get", cpms)

        def dispatch(_, config, run_id):
            drive_site_tool(f, run_id, {"address_terms": terms})
            headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
            publication = f["client"].post(
                "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
            )
            headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run_id}/completions",
                headers=headers,
                json={
                    "status": "completed",
                    "answer": "{{cpms_site_search#/records/0/name}}",
                },
            )
            assert response.status_code == 200, response.text

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "帮我找一下广州天河附近的充电站"}
        )
        final = terminal(f, response.json()["qa_id"])
        assert final.json()["status"] == "completed", final.text
        assert "HuaJing Station" in final.text, (final.text, calls)
        assert [query["address"][0] for query in calls] == terms


@pytest.mark.parametrize(
    "arguments",
    [
        {"address_terms": "广州天河"},
        {"address_terms": ["天河"] * 5},
        {"address_terms": [""]},
        {"address_terms": [1]},
        {"address_terms": ["深圳"]},
        {"address_terms": ["天%河"]},
        {"address": "广州天河", "address_terms": ["广州", "天河"]},
    ],
)
def test_site_plan_refuses_malformed_or_invented_terms(arguments):
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_TOOL_ARGUMENTS_INVALID"):
        operator_cpms.validate_tool_arguments("cpms_site_search", arguments, "帮我找广州天河充电站")


def test_site_plan_freezes_whole_original_terms_and_keeps_legacy_literal():
    assert operator_cpms.validate_tool_arguments(
        "cpms_site_search", {"address_terms": ["NewYork", "NewYork"]}, "Find New York sites"
    ) == {"address_terms": ["New York"]}
    legacy = operator_cpms.tool_site_keywords({"address": "广州天河"})
    assert legacy.address_terms == ("广州天河",)
    assert not legacy.matches({"name": "站A", "address": "天河公园 Guang Zhou"})
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_TOOL_ARGUMENTS_INVALID"):
        operator_cpms.validate_tool_arguments(
            "cpms_site_search", {"address_terms": ["甲" * 50, "乙" * 50]}, "甲" * 50 + "乙" * 50
        )


@pytest.mark.parametrize("fault", ["tenant", "site", "revoked"])
def test_second_term_refusal_never_returns_partial_results(fault):
    scope = operator_cpms.SiteScope("TENANT-A", ("SITE-1",), "scope")
    calls = []

    def read(path, tenant):
        calls.append(path)
        row = {"id": "SITE-1", "tenantId": tenant, "name": "测试站", "address": "广州 天河"}
        if len(calls) == 2:
            if fault == "tenant":
                row["tenantId"] = "OTHER"
            if fault == "site":
                row["id"] = "OTHER"
        return {"code": 0, "data": {"records": [row], "total": 1, "current": 1, "size": 50}}

    def auth():
        return "revoked" if fault == "revoked" and calls else "scope"

    with pytest.raises(operator_cpms.OperatorRefused):
        operator_cpms.read_site_tool(
            scope, "cpms_site_search", {"address_terms": ["广州", "天河"]}, read, auth
        )
    assert len(calls) <= 2


def test_site_plan_deduplicates_and_keeps_node_page_contract():
    scope = operator_cpms.SiteScope("T", None, "scope")
    calls = []

    def read(path, tenant):
        calls.append(parse_qs(urlsplit(path).query))
        rows = [
            {"id": str(i), "tenantId": tenant, "name": "测试站", "address": "广州天河"} for i in range(50)
        ]
        return {"code": 0, "data": {"records": rows, "total": 100, "current": 1, "size": 50}}

    data, response = operator_cpms.read_site_tool(
        scope, "cpms_site_search", {"address_terms": ["广州", "天河"]}, read, lambda: "scope"
    )
    assert len(calls) == 2
    assert len(data["records"]) == 50
    assert data["total"] >= len(data["records"]) and data["truncated"] is True
    assert response["data"]["size"] == 50 and response["data"]["current"] == 1
    assert all(row["tenantId"] == "T" for row in response["data"]["records"])
