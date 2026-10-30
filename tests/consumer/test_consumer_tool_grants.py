"""C 端运行的 CPMS 工具授权（#1209）：范围、参数、请求目标、证据。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aiops_diagnostics.consumer import consumer_sites, consumer_tool_grants
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.operator import operator_cpms, operator_tools

QUESTION = "我充过电的站点有哪些"
TOOL = "cpms_site_search"
RUN = "run_" + "a" * 43
MANIFEST = "b" * 64


def caller():
    subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def run(**overrides):
    base = {
        "run_id": RUN,
        "operation": "charging_status",
        "state": "running",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "scope_json": scope_to_json(caller()),
        "scope_fingerprint": caller().scope_fingerprint,
        "qa_id": "qa_1",
        "app_id": "app",
        "environment": "env-a",
        "expected_node_id": "node-a",
        "expected_agent_version": MANIFEST,
        "execution_manifest": manifest(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def manifest(tools=(TOOL,)):
    return json.dumps(
        {
            "schema": "m3-exec-config/1",
            "name": "n",
            "history_policy": "none",
            "model": {"provider_key": "p", "adapter_key": "a", "model_key": "m", "endpoint": "https://e"},
            "model_options": {},
            "system_messages": ["S"],
            "tools": [
                {"name": name, "description": "d", "input_schema": operator_tools.tool(name).input_schema()}
                for name in tools
            ],
        }
    )


@pytest.fixture
def grants(monkeypatch):
    """一套最小的运行/节点/问答替身。CPMS 与订单列表都是合成的。"""
    order_rows = [{"orderNo": "O-1", "siteId": "SITE-A", "tenantId": "T-1"}]
    site_rows = [
        {"id": "SITE-A", "tenantId": "T-1", "name": "本人的站", "address": "广州天河"},
        {"id": "SITE-B", "tenantId": "T-1", "name": "别人的站", "address": "广州越秀"},
    ]

    def fake_get(url, tenant, c_user_id, **_kwargs):
        assert c_user_id == "C-1", "C 端请求必须带会话推导出的用户标识"
        if consumer_sites.ORDER_PAGE_PATH in url:
            return {"code": 0, "data": {"records": order_rows, "total": 1, "current": 1, "size": 50}}
        return {"code": 0, "data": {"records": site_rows, "total": 2, "current": 1, "size": 50}}

    monkeypatch.setattr(consumer_sites, "_get", fake_get)
    node = SimpleNamespace(node_id="node-a", environment="env-a")
    config = SimpleNamespace(
        expected_node_id="node-a",
        environment="env-a",
        authenticate_node=lambda token: node if token == "node-token" else None,
    )
    monkeypatch.setattr(
        "aiops_diagnostics.execution.chat_execution.callback_config", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        "aiops_diagnostics.execution.node_publication.all_targets",
        lambda _settings: [
            SimpleNamespace(
                app_id="app",
                environment="env-a",
                proxy_base_url="https://gateway.example",
            )
        ],
    )
    context = SimpleNamespace(
        agent_run_store=SimpleNamespace(get=lambda _id: run(), peek=lambda _id: run()),
        store=SimpleNamespace(
            get_assistant_question=lambda *_a: {"question": QUESTION},
        ),
    )
    settings = SimpleNamespace(charging_status_base_url="https://cpms.test", public_base_url="")
    return consumer_tool_grants.ConsumerToolGrants(context, settings)


def grant(grants, arguments=None):
    return grants.grant(RUN, "node-token", MANIFEST, TOOL, operator_cpms.TOOL_CONTRACT, arguments)


def test_the_scope_is_the_callers_own_orders_not_the_tenant(grants):
    """范围来自本人订单，与 GET /v1/sites 同一个函数、同一个结果。"""
    body = grant(grants)
    direct = consumer_sites.own_site_scope(
        "T-1", "C-1", consumer_sites.order_reader("https://cpms.test", "T-1", "C-1")
    )
    assert body["site_ids"] == list(direct.site_ids) == ["SITE-A"]
    assert body["base_url"].endswith("/v1/internal/consumer-tools/" + RUN)


def test_only_the_granted_request_target_is_accepted(grants):
    body = grant(grants)
    request = body["request"]
    token = body["bearer"]
    page = grants.proxy(RUN, token, request["path"], request["query"])
    # 租户里有两个站点，本人只有一个：范围外的行被丢掉，不是整份拒绝。
    assert [row["id"] for row in page["records"]] == ["SITE-A"]
    with pytest.raises(consumer_tool_grants.ConsumerToolRefused) as refused:
        grants.proxy(RUN, token, request["path"], "current=2&size=50")
    assert refused.value.code == "CONSUMER_PATH_REJECTED"


def test_arguments_must_come_from_the_question(grants):
    body = grant(grants, {"address": "站点"})
    assert body["request"]["query"].endswith("address=%E7%AB%99%E7%82%B9")
    with pytest.raises(operator_cpms.OperatorRefused):
        grant(grants, {"address": "深圳南山"})


def test_a_tool_that_does_not_serve_this_run_type_is_refused(grants):
    """订单诊断工具没有订单就没有取证对象：C 端运行里连声明都不该过。"""
    with pytest.raises(consumer_tool_grants.ConsumerToolRefused) as refused:
        grants.grant(RUN, "node-token", MANIFEST, "cpms_order_detail", operator_cpms.TOOL_CONTRACT, None)
    assert refused.value.code == "CONSUMER_TOOL_DISABLED"


def test_a_tool_missing_from_the_manifest_is_refused(grants):
    """清单没勾这个工具就不能领 —— 与管家那条路同一条判据。"""
    without_site = json.dumps(
        {
            "schema": "m3-exec-config/1",
            "name": "n",
            "history_policy": "none",
            "model": {"provider_key": "p", "adapter_key": "a", "model_key": "m", "endpoint": "https://e"},
            "model_options": {},
            "system_messages": ["S"],
            "tools": [
                {"name": "charging_status_snapshot", "description": "d", "input_schema": "{}"},
            ],
        }
    )
    grants.context.agent_run_store.get = lambda _id: run(execution_manifest=without_site)
    with pytest.raises(consumer_tool_grants.ConsumerToolRefused) as refused:
        grant(grants)
    assert refused.value.code == "CONSUMER_TOOL_DISABLED"


def test_evidence_must_equal_what_the_gateway_projected(grants):
    body = grant(grants)
    request = body["request"]
    page = grants.proxy(RUN, body["bearer"], request["path"], request["query"])
    evidence = {
        "tool": TOOL,
        "inputs": body["inputs"],
        "source": "cpms.sitePage",
        "observed_at": datetime.now(UTC).isoformat(),
        "data": page,
    }
    accepted = grants.record(
        RUN,
        body["bearer"],
        MANIFEST,
        TOOL,
        {"authorization_id": body["authorization_id"], "evidence": evidence},
    )
    assert accepted["accepted"] is True
    for broken in (
        {**evidence, "data": {**page, "total": 999}},
        {**evidence, "source": "cpms.other"},
        {k: v for k, v in evidence.items() if k != "inputs"},
    ):
        with pytest.raises(consumer_tool_grants.ConsumerToolRefused):
            grants.record(
                RUN,
                body["bearer"],
                MANIFEST,
                TOOL,
                {"authorization_id": body["authorization_id"], "evidence": broken},
            )


def test_a_wrong_node_token_is_refused_before_anything_else(grants):
    with pytest.raises(consumer_tool_grants.ConsumerToolRefused) as refused:
        grants.grant(RUN, "wrong-token", MANIFEST, TOOL, operator_cpms.TOOL_CONTRACT, None)
    assert refused.value.code == "CONSUMER_NODE_REJECTED"
