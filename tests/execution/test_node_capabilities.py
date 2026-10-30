"""启用/恢复之前核对实际执行节点协议，不以在线或构建标签代替兼容性。"""

import hashlib
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from dify.test_self_service import managed  # noqa: F401
from operations.test_operator_cpms import operator  # noqa: F401

from aiops_diagnostics.dify.self_service_identity import SetupRefused
from aiops_diagnostics.execution.agent_runs import AgentRunConfig
from aiops_diagnostics.execution.node_capabilities import record


def record_ready(database, config_path):
    config = AgentRunConfig.load(Path(config_path))
    node = next(n for n in config.node_token_hashes.values() if n.node_id == config.expected_node_id)
    payload = {
        "schemas": ["m3-exec-config/1", "operator-exec-config/1", "operator-conversation/1"],
        "tool_contract": "request-target/1",
        "tools_sha256": hashlib.sha256(
            files("aiops_diagnostics.operator").joinpath("operator_tools.json").read_bytes()
        ).hexdigest(),
    }
    record(database, config, node, payload)
    return payload


@pytest.mark.parametrize("bad", ["schema", "tools", "expired"])
def test_incompatible_executor_cannot_publish_or_restore(managed, bad):  # noqa: F811
    f, service, caller, _, _ = managed
    before = service.route("TENANT-A")
    service.publish(caller, before.app_id, before.revision)
    before = service.route("TENANT-A")
    payload = record_ready(f["settings"].database_file, f["target"].config_file)
    if bad == "schema":
        payload["schemas"] = ["legacy/1"]
    if bad == "tools":
        payload["tools_sha256"] = "0" * 64
    with sqlite3.connect(f["settings"].database_file) as db:
        db.execute("UPDATE execution_node_capabilities SET body=?", (json.dumps(payload),))
        if bad == "expired":
            db.execute("UPDATE execution_node_capabilities SET observed_at=0")
    for action in [
        lambda: service.publish(caller, before.app_id, before.revision),
        lambda: service.change_version(caller, before.app_id, before.revision, before.agent_version_no),
    ]:
        with pytest.raises(SetupRefused, match="SETUP_EXECUTOR_"):
            action()
        assert service.route("TENANT-A") == before
    record_ready(f["settings"].database_file, f["target"].config_file)
    assert service.publish(caller, before.app_id, before.revision)["version_no"] >= 1


def test_wrong_node_token_cannot_announce_capabilities(operator):  # noqa: F811
    f = operator
    f["settings"].agent_run_config_file = f["target"].config_file
    response = f["client"].post(
        "/v1/internal/execution-node/capabilities",
        headers={"Authorization": "Bearer forged"},
        json=record_ready(f["settings"].database_file, f["target"].config_file),
    )
    assert response.status_code == 403
