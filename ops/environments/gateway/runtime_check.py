"""由候选venv以服务身份执行；只读检查原库，配置启动验证仅使用新状态根。"""

import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import dotenv_values

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import JOB_TABLES


def check(config, state, environment, *, existing, managed_state=None, candidate_registry=None, runtime=None):
    values = dotenv_values(config, interpolate=False)
    forbidden = {
        "AIOPS_HOME",
        "AIOPS_DATA_HOME",
        "AIOPS_CONFIG_HOME",
        "AIOPS_CONFIG_FILE",
        "AIOPS_GATEWAY_DATA_HOME",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_AGENT_RUN_ROOT",
        "AIOPS_CODEX_KEY_DIR",
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
        "AIOPS_GATEWAY_ENVIRONMENT",
    }
    if {key for key in forbidden if values.get(key)}:
        raise ValueError("deployment identity and writable paths belong to installer")
    if candidate_registry is not None:
        from aiops_diagnostics.gateway.environment_projection import validate_candidate

        validate_candidate(config, candidate_registry, environment, runtime=runtime)
    database = state / "gateway/gateway.db"
    route = values.get("AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE")
    if route and Path(route) != (managed_state or state) / "gateway/gateway.db":
        raise ValueError("route database must share managed database")
    if existing:
        report = gateway_schema.check(database)
        if report["version"] != report["current_version"]:
            raise ValueError("schema migration requires separate delivery")
        gateway_schema.integrity(database)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            rows = [(t, "status", ("queued", "running")) for t in JOB_TABLES]
            rows += [
                ("agent_runs", "state", ("issued", "running")),
                ("chat_dify_runs", "state", ("running", "running")),
                ("operator_setup_jobs", "state", ("pending", "running")),
            ]
            for table, column, active in rows:
                if db.execute(f"SELECT 1 FROM {table} WHERE {column} IN (?, ?) LIMIT 1", active).fetchone():
                    raise ValueError("inflight work prevents upgrade")
        return report
    # 调用者给全新状态目录；不对现役库调用validate（它可能迁移或初始化）。
    os.environ.update(
        AIOPS_HOME=str(state),
        AIOPS_DATA_HOME=str(state),
        AIOPS_CONFIG_HOME=str(state / "config"),
        AIOPS_GATEWAY_DATABASE_FILE=str(database),
        AIOPS_GATEWAY_ENVIRONMENT=environment,
        AIOPS_GATEWAY_SERVER_CONFIG_FILE=str(config),
        AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE="",
    )
    settings = GatewayServerSettings.from_env()
    if route:
        settings.chat_route_bindings_file = str(database)
    # 真正validate由随后隔离网络/状态的候选服务执行，预检不触发外部依赖操作。
    Settings.from_config(config).agent.validate()
    return {"configuration": "valid"}


if __name__ == "__main__":
    try:
        print(
            json.dumps(
                check(
                    Path(sys.argv[1]),
                    Path(sys.argv[2]),
                    sys.argv[3],
                    existing=sys.argv[4] == "existing",
                    managed_state=Path(sys.argv[5]) if len(sys.argv) > 5 else None,
                    candidate_registry=Path(sys.argv[6]) if len(sys.argv) > 6 else None,
                    runtime=Path(sys.argv[7]) if len(sys.argv) > 7 else None,
                )
            )
        )
    except Exception as exc:
        print(json.dumps({"result": "refused", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
