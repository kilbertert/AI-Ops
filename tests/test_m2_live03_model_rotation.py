"""模型凭据轮换：成功路径要**读回核对**，失败路径要**拒绝**而不是报成功。

用替身替换 `call()`，而不是启动平台：这里钉住的是「读回的结论如何影响退出」，
包括读不到、读到空、读到脱敏值三种不该被当成成功的情形。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "deploy/m2-live-03/bootstrap_platform.py"


@pytest.fixture
def bootstrap(monkeypatch):
    spec = importlib.util.spec_from_file_location("m2live_bootstrap_rotation", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # `fail()` 走 SystemExit，便于断言「拒绝」而不是「恰好没抛异常」。
    return module


def _config():
    return {
        "id": 7,
        "providerId": 1,
        "modelName": "aiops-m2live03",
        "modelKey": "aiops-m2live03",
        "modelType": "CHAT",
        "adapterKey": "openai-compatible",
        "apiEndpoint": "https://api.example.test/v1",
        "isDefault": True,
    }


def test_rotation_writes_when_the_stored_key_differs(bootstrap, monkeypatch):
    calls = []

    def fake_call(path, body=None, headers=None, method=None):
        calls.append((method or "GET", path))
        if method == "PUT":
            return 200, {"status": 1}
        if not calls[:-1] or calls[0][0] == "GET" and len(calls) == 1:
            return 200, {"status": 1, "data": {"apiKey": "old-key"}}
        return 200, {"status": 1, "data": {"apiKey": "new-key"}}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "new-key")
    assert any(method == "PUT" for method, _ in calls)


def test_rotation_is_a_no_op_when_the_stored_key_already_matches(bootstrap, monkeypatch):
    calls = []

    def fake_call(path, body=None, headers=None, method=None):
        calls.append((method or "GET", path))
        return 200, {"status": 1, "data": {"apiKey": "same-key"}}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "same-key")
    assert not any(method == "PUT" for method, _ in calls)


def test_an_unreadable_config_before_rotation_is_refused(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap, "call", lambda *a, **k: (503, {"status": 0}))
    with pytest.raises(SystemExit):
        bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "new-key")


def test_an_unreadable_config_after_rotation_is_refused(bootstrap, monkeypatch):
    """PUT 成功但读不回来：不能报成功 —— 那就等于没有核对。"""
    state = {"get": 0}

    def fake_call(path, body=None, headers=None, method=None):
        if method == "PUT":
            return 200, {"status": 1}
        state["get"] += 1
        if state["get"] == 1:
            return 200, {"status": 1, "data": {"apiKey": "old-key"}}
        return 503, {"status": 0}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    with pytest.raises(SystemExit):
        bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "new-key")


def test_a_masked_key_cannot_be_verified_and_is_refused(bootstrap, monkeypatch):
    """平台若改成返回脱敏值，我们无法证明轮换生效 —— 宁可失败，不报未经证实的成功。"""
    state = {"get": 0}

    def fake_call(path, body=None, headers=None, method=None):
        if method == "PUT":
            return 200, {"status": 1}
        state["get"] += 1
        key = "old-key" if state["get"] == 1 else "sk-****abcd"
        return 200, {"status": 1, "data": {"apiKey": key}}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    with pytest.raises(SystemExit):
        bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "new-key")


def test_an_empty_key_after_rotation_is_refused(bootstrap, monkeypatch):
    state = {"get": 0}

    def fake_call(path, body=None, headers=None, method=None):
        if method == "PUT":
            return 200, {"status": 1}
        state["get"] += 1
        return 200, {"status": 1, "data": {"apiKey": "" if state["get"] > 1 else "old-key"}}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    with pytest.raises(SystemExit):
        bootstrap.rotate_model_key("t", _config(), "https://api.example.test/v1", "new-key")


def test_endpoint_mismatch_is_reported_as_such(bootstrap, monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "call",
        lambda *a, **k: (
            200,
            {"status": 1, "data": [dict(_config(), apiEndpoint="https://other.test/v1")]},
        ),
    )
    with pytest.raises(SystemExit):
        bootstrap.ensure_model("t", "https://api.example.test/v1", "k", "vendor-model")


def test_a_missing_model_id_is_refused_before_touching_the_platform(bootstrap, monkeypatch):
    """缺供应商模型标识时必须在**改平台状态之前**拒绝，不能先建一个不认识的 modelKey。"""
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "call", lambda path, *a, **k: calls.append(path) or (200, {}))
    with pytest.raises(SystemExit):
        bootstrap.ensure_model("t", "https://api.example.test/v1", "k", "")
    assert calls == [], "缺模型标识时不应该发出任何平台请求"


def test_an_internal_name_left_as_model_key_is_refused(bootstrap, monkeypatch):
    """上一版把项目名写成了 modelKey：供应商会收到一个不认识的模型标识。"""
    monkeypatch.setattr(
        bootstrap,
        "call",
        lambda *a, **k: (
            200,
            {"status": 1, "data": [dict(_config(), modelKey=bootstrap.SERVICE)]},
        ),
    )
    with pytest.raises(SystemExit):
        bootstrap.ensure_model("t", "https://api.example.test/v1", "k", "vendor-model")


def test_the_created_config_carries_the_vendor_model_id(bootstrap, monkeypatch):
    created: dict = {}

    def fake_call(path, body=None, headers=None, method=None):
        if path == "/ai-model/by-type/CHAT":
            return 200, {"status": 1, "data": []}
        if path == "/ai-model/providers":
            return 200, {"status": 1, "data": [{"id": 1, "providerKey": "openai"}]}
        created.update(body or {})
        return 200, {"status": 1, "data": 9}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    bootstrap.ensure_model("t", "https://api.example.test/v1", "k", "vendor-model")
    assert created["modelKey"] == "vendor-model", "发给供应商的 model 必须是供应商标识"
    assert bootstrap.SERVICE in created["modelName"], "内部资源名只应出现在显示名里"
