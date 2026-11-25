"""由候选venv以服务身份执行；只读检查原库，配置启动验证仅使用新状态根。"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import dotenv_values

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.gateway import gateway_db, gateway_schema
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import JOB_TABLES


def check(
    config,
    state,
    environment,
    *,
    existing,
    allow_schema_upgrade=False,
    managed_state=None,
    candidate_registry=None,
    runtime=None,
    previous_directory=None,
):
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
        from aiops_diagnostics.onboarding.projection import validate_candidate

        validate_candidate(
            config, candidate_registry, environment, runtime=runtime, previous_directory=previous_directory
        )
    database = state / "gateway/gateway.db"
    route = values.get("AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE")
    if route and Path(route) != (managed_state or state) / "gateway/gateway.db":
        raise ValueError("route database must share managed database")
    if existing:
        report = gateway_schema.check(database)
        if report["version"] > report["current_version"]:
            raise ValueError("schema downgrade refused")
        if not allow_schema_upgrade and report["version"] != report["current_version"]:
            raise ValueError("schema migration requires separate delivery")
        gateway_schema.integrity(database)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            existing_tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            legacy_names = {
                new: old for old, new in getattr(gateway_schema, "LEGACY_TABLE_RENAMES", {}).items()
            }
            rows = [(t, "status", ("queued", "running")) for t in JOB_TABLES]
            rows += [
                ("agent_runs", "state", ("issued", "running")),
                ("chat_dify_runs", "state", ("running", "running")),
                ("operator_setup_jobs", "state", ("pending", "running")),
            ]
            for table, column, active in rows:
                physical = table
                if allow_schema_upgrade and physical not in existing_tables:
                    legacy = legacy_names.get(table)
                    if legacy and legacy in existing_tables:
                        physical = legacy
                    elif report["version"] < report["current_version"]:
                        continue
                if db.execute(
                    f'SELECT 1 FROM "{physical}" WHERE {column} IN (?, ?) LIMIT 1', active
                ).fetchone():
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


def rehearse_upgrade(config, state, environment, backup_path):
    """在独立临时副本上演练已知旧版 schema 到当前版的前向迁移，不修改现役库。"""
    report = check(config, state, environment, existing=True, allow_schema_upgrade=True)
    if report["version"] == report["current_version"]:
        return {
            "rehearsed": False,
            "version": report["version"],
            "current_version": report["current_version"],
        }
    database = state / "gateway/gateway.db"
    migrated = gateway_schema.rehearse(database, Path(backup_path))
    return {
        "rehearsed": True,
        "from_version": report["version"],
        "to_version": migrated["version"],
        "current_version": migrated["current_version"],
    }


def backup_database(config, state, environment, backup_path):
    """在停服切换窗内以服务身份做一致性 SQLite `.backup()` 快照，返回摘要。"""
    report = check(config, state, environment, existing=True, allow_schema_upgrade=True)
    database = state / "gateway/gateway.db"
    target = Path(backup_path)
    ensure_private_directory(target.parent)
    with target.open("xb"):
        pass
    protect_private_file(target)
    with gateway_db.connection(database, readonly=True) as source, gateway_db.connection(target) as dest:
        source.backup(dest)
    protect_private_file(target)
    with target.open("rb") as stream:
        os.fsync(stream.fileno())
        stream.seek(0)
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "version": report["version"],
        "current_version": report["current_version"],
        "backup_sha256": sha256,
    }


def migrate_database(config, state, environment):
    """在停服且备份已落账本后，单事务执行已知旧版到当前版的前向 schema 迁移并复核完整性。"""
    report = check(config, state, environment, existing=True, allow_schema_upgrade=True)
    if report["version"] == report["current_version"]:
        return {
            "migrated": False,
            "from_version": report["version"],
            "to_version": report["current_version"],
        }
    database = state / "gateway/gateway.db"
    migrated = gateway_schema.startup(database)
    with gateway_db.connection(database) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gateway_schema.integrity(database)
    return {
        "migrated": True,
        "from_version": report["version"],
        "to_version": migrated["version"],
    }


def read_database_version(state):
    """以服务身份只读查询现役库 PRAGMA user_version，避免 root 接触 WAL 侧文件。"""
    database = state / "gateway/gateway.db"
    with gateway_db.connection(database, readonly=True) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return {"version": version}


if __name__ == "__main__":
    try:
        mode = sys.argv[4]
        if mode == "existing":
            output = check(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], existing=True)
        elif mode == "upgrade-check":
            output = check(
                Path(sys.argv[1]),
                Path(sys.argv[2]),
                sys.argv[3],
                existing=True,
                allow_schema_upgrade=True,
            )
        elif mode == "rehearse":
            output = rehearse_upgrade(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[5]))
        elif mode == "backup":
            output = backup_database(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[5]))
        elif mode == "migrate":
            output = migrate_database(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
        elif mode == "version":
            output = read_database_version(Path(sys.argv[2]))
        else:
            output = check(
                Path(sys.argv[1]),
                Path(sys.argv[2]),
                sys.argv[3],
                existing=False,
                managed_state=Path(sys.argv[5]) if len(sys.argv) > 5 else None,
                candidate_registry=Path(sys.argv[6]) if len(sys.argv) > 6 else None,
                runtime=Path(sys.argv[7]) if len(sys.argv) > 7 else None,
                previous_directory=Path(sys.argv[8]) if len(sys.argv) > 8 else None,
            )
        print(json.dumps(output))
    except Exception as exc:
        print(json.dumps({"result": "refused", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
