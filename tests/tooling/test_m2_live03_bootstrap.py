"""bootstrap 的两条硬约束：节点摘要必须由**同一个**令牌派生，且默认入口必须被处置。

这些是纯函数级回归（不启动平台）：它们钉住的是「绑定表与 node.env 不能各说各话」
这个形状，而那正是节点心跳被拒的根因。
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "deploy/m2-live-03/bootstrap_platform.py"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("m2live_bootstrap", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binding_digest_is_derived_from_the_active_token(bootstrap):
    """首次部署时绑定表里的旧摘要必须被**当前**令牌替换，否则节点永远领不到。"""
    bindings = {
        "nodes": [
            {"node_id": "node-41", "environment": "live-trial-41", "token_sha256": "0" * 64},
            {"node_id": "node-other", "environment": "live-trial-41", "token_sha256": "1" * 64},
        ]
    }
    token = "a" * 64
    updated = bootstrap.bind_expected_node(bindings, token)
    expected = hashlib.sha256(token.encode()).hexdigest()

    by_id = {item["node_id"]: item for item in updated["nodes"]}
    assert by_id["node-41"]["token_sha256"] == expected
    assert by_id["node-41"]["environment"] == bootstrap.ENVIRONMENT
    # 其他节点条目保留：撤销/轮换由运维改这张表，bootstrap 不替他们做决定。
    assert by_id["node-other"]["token_sha256"] == "1" * 64


def test_binding_update_is_idempotent(bootstrap):
    first = bootstrap.bind_expected_node({"nodes": []}, "b" * 64)
    second = bootstrap.bind_expected_node(first, "b" * 64)
    assert first == second
    assert len(second["nodes"]) == 1


def test_the_saved_binding_matches_what_the_node_will_use(bootstrap):
    """同一令牌两处派生必须逐字相等 —— 这是心跳与领取都依赖的那一条。"""
    token = "c" * 32
    bindings = bootstrap.bind_expected_node({"nodes": []}, token)
    assert bindings["nodes"][0]["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()


def test_node_env_regeneration_keeps_non_credential_keys(tmp_path, bootstrap, monkeypatch):
    """bootstrap 只重写三行凭据，其余开关必须原样保留。

    早先整份覆盖：`AIOPS_EXEC_MANIFEST` 在 bootstrap 之后**静默消失** ——
    看起来配了守卫、实际已关，而链路仍然跑通。这条直接驱动真实的重写函数。
    """
    existing = tmp_path / "node.env.in"
    existing.write_text(
        "SNAIL_AI_APP_ID=old-app\n"
        "SNAIL_AI_TOKEN=old-token\n"
        "AIOPS_NODE_TOKEN=old-node\n"
        "AIOPS_EXEC_MANIFEST=/etc/m2live/exec-manifest.json\n",
        encoding="utf-8",
    )
    out = tmp_path / "node.env.out"
    # `NODE_ENV_PATH` / `NODE_ENV_OUT` 是模块级常量（容器里的固定路径），
    # 用 `reload` 换成 tmp 路径 —— 直接 patch 常量不会影响已编译进函数体的引用。
    monkeypatch.setattr(bootstrap, "NODE_ENV_PATH", str(existing))
    monkeypatch.setattr(bootstrap, "NODE_ENV_OUT", str(out))

    bootstrap._write_node_env(
        {"SNAIL_AI_APP_ID": "new-app", "SNAIL_AI_TOKEN": "new-token", "AIOPS_NODE_TOKEN": "new-node"}
    )

    text = out.read_text(encoding="utf-8")
    assert "AIOPS_EXEC_MANIFEST=/etc/m2live/exec-manifest.json" in text, "非凭据开关被抹掉了"
    assert "SNAIL_AI_APP_ID=new-app" in text
    assert "old-app" not in text
    assert out.stat().st_mode & 0o777 == 0o600


def _agent_page(agent: dict) -> dict:
    return {"status": 1, "data": [agent]}


_AGENT = {
    "id": 5,
    "name": "aiops-m2live03",
    "instruction": "旧指令",
    "chatModelId": 7,
    "appId": "app-1",
    "mcpEnabled": False,
}


def test_an_agent_on_another_model_config_is_rebound(bootstrap, monkeypatch):
    """换了供应商会新建一条模型配置；智能体不改绑就仍跑旧模型。

    这正是「配置改了、跑的没变」的形态：`ensure_model` 按 modelKey 找行，
    换标识新建行，而 `ensure_agent` 早先只按名字找到就返回，从不改 `chatModelId`。
    """
    calls: list[tuple[str, str, dict | None]] = []
    current = dict(_AGENT)

    def fake_call(path, body=None, headers=None, method=None):
        verb = method or ("GET" if body is None else "POST")
        calls.append((verb, path, body))
        if path.startswith("/agent/page"):
            return 200, _agent_page(dict(current))
        if verb == "GET":
            return 200, {"status": 1, "data": dict(current)}
        if verb == "PUT":
            current.update(body)
        return 200, {"status": 1}

    monkeypatch.setattr(bootstrap, "call", fake_call)

    assert bootstrap.ensure_agent("tok", "app-1", 42) == 5

    writes = [c for c in calls if c[0] == "PUT"]
    assert writes, "指向别的模型配置时必须改绑，而不是按名字直接返回"
    assert writes[0][1] == "/agent/5"
    assert writes[0][2]["chatModelId"] == 42
    assert current["instruction"] == bootstrap.AGENT_INSTRUCTION
    assert current["appId"] == "app-1"
    # 读回核对：写入后必须再读一次，不能凭写入的返回码认为成功。
    assert [c[0] for c in calls][-1] == "GET"


def test_an_agent_already_on_this_model_config_is_left_alone(bootstrap, monkeypatch):
    calls: list[str] = []
    current = dict(_AGENT, chatModelId=42, instruction=bootstrap.AGENT_INSTRUCTION)

    def fake_call(path, body=None, headers=None, method=None):
        calls.append(method or ("GET" if body is None else "POST"))
        return 200, _agent_page(current)

    monkeypatch.setattr(bootstrap, "call", fake_call)

    assert bootstrap.ensure_agent("tok", "app-1", 42) == 5
    assert "PUT" not in calls


def test_a_rebind_that_does_not_take_is_refused(bootstrap, monkeypatch):
    """写成功但读回仍是旧模型 ⇒ 拒绝，而不是报成功。

    `fail()` 是 SystemExit：这条断言的是「拒绝」，不是「恰好没抛」。
    """
    state = {"model": 7}

    def fake_call(path, body=None, headers=None, method=None):
        verb = method or ("GET" if body is None else "POST")
        if path.startswith("/agent/page"):
            return 200, _agent_page(dict(_AGENT))
        if verb == "PUT":
            return 200, {"status": 1}  # 平台回 200，但不生效
        return 200, {"status": 1, "data": dict(_AGENT, chatModelId=state["model"])}

    monkeypatch.setattr(bootstrap, "call", fake_call)

    with pytest.raises(SystemExit):
        bootstrap.ensure_agent("tok", "app-1", 42)


def test_rebind_refuses_an_agent_with_another_name(bootstrap, monkeypatch):
    """按名字找到才改：名字对不上时绝不改写别人的智能体。"""

    def fake_call(path, body=None, headers=None, method=None):
        verb = method or ("GET" if body is None else "POST")
        if path.startswith("/agent/page"):
            return 200, _agent_page(dict(_AGENT, name="someone-else"))
        if verb == "GET":
            return 200, {"status": 1, "data": dict(_AGENT, name="someone-else")}
        return 200, {"status": 1}

    monkeypatch.setattr(bootstrap, "call", fake_call)

    with pytest.raises(SystemExit):
        bootstrap.rebind_agent("tok", dict(_AGENT, name="aiops-m2live03"), "app-1", 42)


@pytest.mark.parametrize("drift", ["instruction", "appId", "envelope"])
def test_rebind_requires_all_written_fields_to_read_back(bootstrap, monkeypatch, drift):
    current = dict(_AGENT)

    def fake_call(path, body=None, headers=None, method=None):
        if method == "PUT":
            current.update(body)
            if drift == "instruction":
                current["instruction"] = "旧指令"
            if drift == "appId":
                current["appId"] = "another-app"
            return 200, {"status": 1}
        envelope = 0 if drift == "envelope" and current["chatModelId"] == 42 else 1
        return 200, {"status": envelope, "data": dict(current)}

    monkeypatch.setattr(bootstrap, "call", fake_call)
    with pytest.raises(SystemExit):
        bootstrap.rebind_agent("tok", dict(_AGENT), "app-1", 42)
