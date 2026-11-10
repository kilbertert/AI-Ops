"""原同源 HTTP 桥的日志白名单与 Dify 应用授权。"""

import json

import pytest

from dify.test_dify_console_publish import APP_ID, _browser_headers, _post, bridge, upstream  # noqa: F401


def request(address, **body):
    return _post(
        address,
        path="/__aiops/manage",
        headers=_browser_headers(),
        body=json.dumps({"action": "run-logs", "app_id": APP_ID, **body}).encode(),
    )


@pytest.mark.parametrize(
    "action,fields,path",
    [
        ("run-logs", {"page": 2, "limit": 20}, "/run-logs"),
        ("run-log", {"run_id": "run_" + "a" * 43}, "/run-logs/detail"),
    ],
)
def test_logs_cross_real_bridge(bridge, upstream, action, fields, path):  # noqa: F811
    request(bridge, action=action, **fields)
    calls = upstream.calls
    assert any(c["path"] == "/console/api/apps/" + APP_ID for c in calls)
    forwarded = [c for c in calls if c["path"] == "/v1/dify/management" + path]
    assert len(forwarded) == 1
    assert forwarded[0]["body"] == {"app_id": APP_ID, **fields}


@pytest.mark.parametrize(
    "fields",
    [
        {"limit": True},
        {"page": 1001},
        {"limit": 51},
        {"run_id": "run_x"},
        {"order_no": "ORDER"},
        {"app_id": None},
        {"action": "run-log", "run_id": "../bad"},
        {"action": "status", "page": 1},
    ],
)
def test_invalid_fields_never_reach_gateway(bridge, upstream, fields):  # noqa: F811
    code, _ = request(bridge, **fields)
    assert code == 400
    assert not any(c["path"].startswith("/v1/dify/") for c in upstream.calls)


def test_revoked_dify_app_never_reaches_gateway(bridge, upstream):  # noqa: F811
    upstream.app_status = 403
    code, _ = request(bridge)
    assert code == 403
    assert not any(c["path"].startswith("/v1/dify/") for c in upstream.calls)
