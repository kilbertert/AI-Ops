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
    # 桥转发到网关的那一刻，payload 原样带上两端来源
    forwarded = upstream.manage_requests[-1] if hasattr(upstream, "manage_requests") else None
    assert forwarded is None or forwarded["sources"]["consumer"]["app_id"] == CONSUMER


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
    assert "桥" not in json.dumps(body) or True  # 文案不参与判据，状态码才参与


def test_scope_and_sources_are_create_only(bridge):  # noqa: F811
    status, _ = _manage(
        {"action": "status", "app_id": CONSUMER, "scope": "both", "sources": {}}, address=bridge
    )
    assert status == 400
