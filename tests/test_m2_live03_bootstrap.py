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
