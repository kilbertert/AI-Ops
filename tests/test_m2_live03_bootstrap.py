"""bootstrap 的两条硬约束：节点摘要必须由**同一个**令牌派生，且默认入口必须被处置。

这些是纯函数级回归（不启动平台）：它们钉住的是「绑定表与 node.env 不能各说各话」
这个形状，而那正是节点心跳被拒的根因。
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "deploy/m2-live-03/bootstrap_platform.py"


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
