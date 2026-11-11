"""节点用自身凭据定期报告实际协议；发布前确认执行器能运行候选版本。"""

import hashlib
import json
import sqlite3
import time
from importlib.resources import files
from pathlib import Path

from aiops_diagnostics.execution.agent_runs import AgentRunConfig
from aiops_diagnostics.gateway import gateway_db

MAX_AGE = 120


def record(database, config, node, payload):
    # 建表 + upsert 是多语句写——LEASE（gateway_db，#573）。observed_at 是
    # epoch 秒列，不是 ISO 存储文本，不在 scalar_rules 的读取范围。
    with gateway_db.initialize(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS execution_node_capabilities (
            environment TEXT NOT NULL,node_id TEXT NOT NULL,body TEXT NOT NULL,observed_at REAL NOT NULL,
            PRIMARY KEY(environment,node_id))""")
        db.execute(
            "INSERT INTO execution_node_capabilities VALUES(?,?,?,?) "
            "ON CONFLICT(environment,node_id) DO UPDATE SET "
            "body=excluded.body,observed_at=excluded.observed_at",
            (config.environment, node.node_id, json.dumps(payload, sort_keys=True), time.time()),
        )


def require_compatible(database, target, manifest):
    from aiops_diagnostics.dify.self_service_identity import SetupRefused
    from aiops_diagnostics.operator import operator_tools
    from aiops_diagnostics.operator.operator_cpms import OBJECT_CONTRACT, TOOL_CONTRACT

    config = AgentRunConfig.load(Path(target.config_file))
    if not any(n.active and n.node_id == config.expected_node_id for n in config.node_token_hashes.values()):
        raise SetupRefused("SETUP_EXECUTOR_UNAVAILABLE")
    try:
        with gateway_db.connection(database) as db:
            row = db.execute(
                "SELECT body,observed_at FROM execution_node_capabilities WHERE environment=? AND node_id=?",
                (config.environment, config.expected_node_id),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or time.time() - row[1] > MAX_AGE:
        raise SetupRefused("SETUP_EXECUTOR_UNAVAILABLE")
    body = json.loads(row[0])
    digest = hashlib.sha256(
        files("aiops_diagnostics.operator").joinpath("operator_tools.json").read_bytes()
    ).hexdigest()
    contracts = (
        {OBJECT_CONTRACT}
        if any(operator_tools.tool(t["name"]).kind == "object" for t in manifest.get("tools", []))
        else {TOOL_CONTRACT, OBJECT_CONTRACT}
    )
    if (
        manifest["schema"] not in body.get("schemas", [])
        or body.get("tool_contract") not in contracts
        or body.get("tools_sha256") != digest
    ):
        raise SetupRefused("SETUP_EXECUTOR_INCOMPATIBLE")
