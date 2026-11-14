"""95覆盖文件保持既有网络/凭据边界；真正Compose合并结果另行验收。"""

from pathlib import Path

import yaml


def test_node95_override_has_bounded_resources_without_adding_network_access():
    root = Path(__file__).resolve().parents[2]
    base = yaml.safe_load((root / "deploy/node-runtime/compose.yaml").read_text())
    extra = yaml.safe_load((root / "ops/environments/env95/node-runtime.override.yaml").read_text())
    services = extra["services"]
    assert sum(int(row["mem_limit"].removesuffix("m")) for row in services.values()) == 1408
    assert all(not row.get("ports") and not row.get("networks") for row in services.values())
    assert services["node"]["environment"]["AIOPS_NODE_NAME"] == "node-95"
    assert all(base["networks"][n]["internal"] for n in base["services"]["node"]["networks"])
    assert "--aiops.node.managed-publication=true" in services["node"]["command"]
