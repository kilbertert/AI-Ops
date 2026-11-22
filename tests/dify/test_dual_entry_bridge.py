"""同源桥的固定动作契约：页面实际发出的双端 payload 必须能穿过真实桥。

独立验收 R1 的反例是：新组件发送 `scope`/`sources`，而 `dify/self_service_bridge.py` 的字段
白名单没有它们 —— 真实浏览器点"复制并开通"会在**桥**上收到 400，根本到不了网关。

这里断言的是**两个方向**：

- 页面会发的合法 payload 能穿过（否则功能不可用）；
- 形状不对的 payload 仍然在桥这一层被拒（否则新字段就成了绕过白名单的通道）。
"""

import json

import pytest

from dify.test_dify_console_publish import (  # noqa: F401
    _browser_headers,
    _post,
    bridge,
    upstream,
)

CONSUMER = "11111111-1111-4111-8111-111111111111"
OPERATOR = "22222222-2222-4222-8222-222222222222"


def _manage(body, **overrides):
    return _post(
        overrides.pop("address"),
        path="/__aiops/manage",
        headers=_browser_headers(**overrides),
        body=json.dumps(body).encode(),
    )


def test_candidate_workspace_request_crosses_bridge_but_resource_claims_do_not(bridge, upstream):  # noqa: F811
    body = {"action": "environment-task", "site_task": "task_" + "a" * 32, "tenant_id": "same"}
    status, _ = _manage(body, address=bridge)
    assert status != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[0]["body"] == {"site_task": body["site_task"], "tenant_id": "same"}
    assert (
        _manage(
            {"action": "environment-task", "workspace_task": body["site_task"], "tenant_id": "same"},
            address=bridge,
        )[0]
        != 400
    )
    for extra in ({"workspace_id": "forged"}, {"tenant_id": ""}, {"site_task": "other"}):
        assert _manage({**body, **extra}, address=bridge)[0] == 400
    consumer = {
        "action": "environment-task",
        "connection_task": body["site_task"],
        "tenant_id": "same",
        "source_app_id": CONSUMER,
        "source_version": 7,
    }
    assert _manage(consumer, address=bridge)[0] != 400
    assert _manage({"action": "environment-task", "consumer_sources": True}, address=bridge)[0] != 400
    for extra in (
        {"source_app_id": "https://other.test"},
        {"workflow": {}},
        {"gateway_url": "https://other.test"},
    ):
        assert _manage({**consumer, **extra}, address=bridge)[0] == 400


def test_dual_entry_payload_crosses_the_real_bridge(bridge, upstream):  # noqa: F811
    status, body = _manage(
        {
            "action": "create",
            "scope": "both",
            "sources": {
                "consumer": {"app_id": CONSUMER, "version_no": 3},
                "operator": {"app_id": OPERATOR, "version_no": 7},
            },
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000001",
            "name": "客户B－客服与管家",
        },
        address=bridge,
    )
    assert status != 400, body
    assert "unexpected field" not in json.dumps(body)
    # 桥转发到网关的那一刻，payload 原样带上两端来源。
    # 判据必须落在上游**真的**收到的那条请求上：`_Upstream` 只记 `calls`
    # （含 path/body），没有 `manage_requests`；按属性存在与否取值的写法恒真。
    forwarded = [c for c in upstream.calls if c["path"].startswith("/v1/dify/manage")]
    assert len(forwarded) == 1, [c["path"] for c in upstream.calls]
    sources = forwarded[0]["body"]["sources"]
    assert sources["consumer"]["app_id"] == CONSUMER
    assert sources["operator"]["app_id"] == OPERATOR


def test_candidate_chain_crosses_bridge_and_rejects_forged_inputs(bridge, upstream):  # noqa: F811
    payload = {
        "action": "environment-task",
        "candidate_plan": "plan_" + "b" * 32,
        "tenant_id": "same",
        "source_app_id": CONSUMER,
        "source_version": 7,
    }
    assert _manage(payload, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {k: v for k, v in payload.items() if k != "action"}
    count = len(forwarded)
    for extra in (
        {"candidate_plan": "task_" + "b" * 32},
        {"tenant_id": ""},
        {"source_app_id": "https://other.test"},
        {"source_version": True},
        {"source_version": 0},
        {"workspace_id": "forged"},
        {"action": "status"},
    ):
        assert _manage({**payload, **extra}, address=bridge)[0] == 400
    assert len([c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]) == count


def test_initial_installation_crosses_bridge_only_as_explicit_true(bridge, upstream):  # noqa: F811
    # 「包含初装」字段必须穿过真实桥白名单且原样转发；非true取值在桥层即拒。
    payload = {
        "action": "environment-task",
        "candidate_plan": "plan_" + "c" * 32,
        "tenant_id": "same",
        "source_app_id": CONSUMER,
        "source_version": 7,
        "initial_installation": True,
    }
    assert _manage(payload, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {k: v for k, v in payload.items() if k != "action"}
    count = len(forwarded)
    for value in (False, "yes", 1, None):
        assert _manage({**payload, "initial_installation": value}, address=bridge)[0] == 400
    assert len([c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]) == count


def test_single_entry_payload_crosses_with_matching_scope(bridge):  # noqa: F811
    status, body = _manage(
        {
            "action": "create",
            "scope": "consumer",
            "sources": {"consumer": {"app_id": CONSUMER, "version_no": 1}},
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000002",
            "name": "客户B－客服",
        },
        address=bridge,
    )
    assert status != 400, body


@pytest.mark.parametrize(
    "payload",
    [
        # 范围与来源不一致：scope=both 却只给一端
        {
            "action": "create",
            "scope": "both",
            "sources": {"consumer": {"app_id": CONSUMER, "version_no": 1}},
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000003",
            "name": "x",
        },
        # 未知端
        {
            "action": "create",
            "scope": "both",
            "sources": {
                "consumer": {"app_id": CONSUMER, "version_no": 1},
                "admin": {"app_id": OPERATOR, "version_no": 1},
            },
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000004",
            "name": "x",
        },
        # 来源里多带字段
        {
            "action": "create",
            "scope": "consumer",
            "sources": {"consumer": {"app_id": CONSUMER, "version_no": 1, "tenant_id": "TENANT-C"}},
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000005",
            "name": "x",
        },
        # 版本号不是正整数
        {
            "action": "create",
            "scope": "consumer",
            "sources": {"consumer": {"app_id": CONSUMER, "version_no": "1"}},
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000006",
            "name": "x",
        },
        # 范围本身不认识
        {
            "action": "create",
            "scope": "all",
            "sources": {"consumer": {"app_id": CONSUMER, "version_no": 1}},
            "tenant_id": "TENANT-B",
            "request_key": "review-request-000007",
            "name": "x",
        },
    ],
)
def test_malformed_dual_entry_payload_is_still_rejected_by_the_bridge(bridge, payload):  # noqa: F811
    status, body = _manage(payload, address=bridge)
    assert status == 400


def test_scope_and_sources_are_create_only(bridge):  # noqa: F811
    status, _ = _manage(
        {"action": "status", "app_id": CONSUMER, "scope": "both", "sources": {}}, address=bridge
    )
    assert status == 400


def test_candidate_installation_crosses_bridge_without_deployment_arguments(bridge, upstream):  # noqa: F811
    payload = {"action": "environment-task", "candidate_task": "task_" + "a" * 32, "installation": True}
    assert _manage(payload, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {"candidate_task": payload["candidate_task"], "installation": True}
    count = len(forwarded)
    for extra in (
        {"installation": 1},
        {"installation": "install"},
        {"candidate_task": "unknown"},
        {"app_id": CONSUMER},
        {"action": "status"},
        {"gateway_url": "https://other.test"},
    ):
        assert _manage({**payload, **extra}, address=bridge)[0] == 400
    assert len([c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]) == count


def test_host_verify_task_crosses_bridge_without_extra_arguments(bridge, upstream):  # noqa: F811
    payload = {
        "action": "environment-task",
        "verify_task": "managed-host-identity",
        "plan_id": "plan_" + "b" * 32,
    }
    assert _manage(payload, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {
        "verify_task": "managed-host-identity",
        "plan_id": payload["plan_id"],
    }
    count = len(forwarded)
    for extra in (
        {"verify_task": 1},
        {"verify_task": "with space"},
        {"plan_id": "unknown"},
        {"app_id": CONSUMER},
        {"action": "status"},
        {"gateway_url": "https://other.test"},
    ):
        assert _manage({**payload, **extra}, address=bridge)[0] == 400
    assert len([c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]) == count


def test_registration_task_crosses_bridge_without_target_arguments(bridge, upstream):  # noqa: F811
    payload = {
        "action": "environment-task",
        "installation_task": "task_" + "a" * 32,
        "registration_plan": "plan_" + "b" * 32,
    }
    assert _manage(payload, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {k: v for k, v in payload.items() if k != "action"}
    for extra in (
        {"installation_task": "unknown"},
        {"registration_plan": 1},
        {"action": "status"},
        {"app_id": CONSUMER},
        {"gateway_url": "https://other.test"},
    ):
        assert _manage({**payload, **extra}, address=bridge)[0] == 400


def test_business_validation_crosses_bridge_without_credentials_or_verdict(bridge, upstream):  # noqa: F811
    body = {
        "action": "environment-task",
        "registration_task": "task_" + "a" * 32,
        "validation_plan": "plan_" + "b" * 32,
        "capabilities": ["consumer", "order", "offline"],
        "case_ref": "approved",
    }
    assert _manage(body, address=bridge)[0] != 400
    forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
    assert forwarded[-1]["body"] == {k: v for k, v in body.items() if k != "action"}
    for extra in (
        {"passed": True},
        {"token": "secret"},
        {"case_ref": "../secret"},
        {"capabilities": ["order", "order"]},
        {"action": "status"},
    ):
        assert _manage({**body, **extra}, address=bridge)[0] == 400


def test_environment_publication_and_restore_cross_actual_bridge_with_only_confirmed_scope(bridge, upstream):  # noqa: F811
    publication = {
        "action": "environment-task",
        "validation_task": "task_" + "a" * 32,
        "publication_plan": "plan_" + "b" * 32,
        "confirm_readonly": True,
        "confirm_customer_knowledge": True,
        "acceptance_ref": "readonly-diagnosis",
    }
    for body in (publication, {"action": "environment-task", "restore_publication": "task_" + "c" * 32}):
        assert _manage(body, address=bridge)[0] != 400
        forwarded = [c for c in upstream.calls if c["path"] == "/v1/dify/management/environment-tasks"]
        assert forwarded[-1]["body"] == {k: v for k, v in body.items() if k != "action"}
    for extra in (
        {"confirm_readonly": 1},
        {"confirm_customer_knowledge": False},
        {"passed": True},
        {"app_id": CONSUMER},
        {"action": "status"},
    ):
        assert _manage({**publication, **extra}, address=bridge)[0] == 400
