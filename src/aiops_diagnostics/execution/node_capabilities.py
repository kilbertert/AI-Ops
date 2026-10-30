"""节点用自身凭据定期报告实际协议；发布前确认执行器能运行候选版本。"""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from aiops_diagnostics.execution.agent_runs import AgentRunConfig

MAX_AGE = 120


def record(database, config, node, payload):
    with closing(sqlite3.connect(database)) as db, db:
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
    from aiops_diagnostics.operator.operator_cpms import TOOL_CONTRACT

    config = AgentRunConfig.load(Path(target.config_file))
    if not any(n.active and n.node_id == config.expected_node_id for n in config.node_token_hashes.values()):
        raise SetupRefused("SETUP_EXECUTOR_UNAVAILABLE")
    try:
        with closing(sqlite3.connect(database)) as db:
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
    if (
        manifest["schema"] not in body.get("schemas", [])
        or body.get("tool_contract") != TOOL_CONTRACT
        or body.get("tools_sha256") != digest
    ):
        raise SetupRefused("SETUP_EXECUTOR_INCOMPATIBLE")
