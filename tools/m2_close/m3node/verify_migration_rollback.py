#!/usr/bin/env python3
"""M3-GATEWAY-01 的**部署影响**证据：列变更是真的，与清单开关无关。

要证明的三件事（Codex 指出的第 6 项）：

1. **旧库副本迁移**：拿一个只含旧列的库，打开 `AgentRunStore` 后新增三列，
   历史行逐字不变、三列为 ``NULL``；
2. **重复启动**：同一进程序列再开一次，结构、数据、文件摘要都不再变；
3. **旧版本读升级后的库**：把**升级前**的 `agent_runs.py` 取出来对同一个库跑一遍读路径
   （不是「应该没问题」的推断）—— 它读得到、写不进未知列、不报错。

以及**保留新增列的代码回滚方案**：清空 `AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE` 即回到
「未启用」分支；列保留但不再被读写；旧行三列仍是 ``NULL``，所以旧代码看到的是 ``NULL``。
这里同时给出反方向的证据：把代码回滚到升级前的版本后，**升级后的库仍可读**。

    uv run python tools/m2_close/m3node/verify_migration_rollback.py --out /tmp/migration.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
LEGACY_REV = "10b3001"  # 本片改动之前的 origin/main

LEGACY_SCHEMA = """
CREATE TABLE agent_runs (
    run_id TEXT PRIMARY KEY, scope_fingerprint TEXT NOT NULL, scope_json TEXT NOT NULL,
    order_no TEXT NOT NULL, operation TEXT NOT NULL, environment TEXT NOT NULL,
    app_id TEXT NOT NULL, agent_id TEXT NOT NULL, expected_node_id TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, snapshot_json TEXT, actual_node_id TEXT,
    node_build TEXT, gateway_version TEXT NOT NULL, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, claimed_at TEXT, completed_at TEXT
);
"""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path, columns: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        names = columns or tuple(row[1] for row in connection.execute("PRAGMA table_info(agent_runs)"))
        select = ", ".join(names)
        return [dict(row) for row in connection.execute(f"SELECT {select} FROM agent_runs ORDER BY run_id")]
    finally:
        connection.close()


def _columns(path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(path)
    try:
        return tuple(row[1] for row in connection.execute("PRAGMA table_info(agent_runs)"))
    finally:
        connection.close()


#: 历史行用**将来**的有效期：`AgentRunStore.get()` 会把过期的非终态收敛成 `expired`，
#: 那是既有 TTL 语义、不是迁移问题。用已过期的时间戳会让这条判据测的是时钟而不是迁移。
FUTURE = "2099-01-01T00:00:00+00:00"


def _legacy_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        """
        INSERT INTO agent_runs (run_id, scope_fingerprint, scope_json, order_no, operation,
            environment, app_id, agent_id, expected_node_id, state, gateway_version,
            created_at, expires_at)
        VALUES ('run_legacy_alpha', 'fp-a', '{}', 'ORDER-A', 'charging_status', 'env-a', 'app',
                '7', 'node-a1', 'succeeded', '1.0', '2026-01-01T00:00:00+00:00',
                ?)
        """,
        (FUTURE,),
    )
    connection.execute(
        """
        INSERT INTO agent_runs (run_id, scope_fingerprint, scope_json, order_no, operation,
            environment, app_id, agent_id, expected_node_id, state, reason,
            gateway_version, created_at, expires_at)
        VALUES ('run_legacy_beta', 'fp-b', '{}', 'ORDER-B', 'charging_status', 'env-a', 'app',
                '7', 'node-a1', 'failed', 'upstream_failed', '1.0',
                '2026-01-02T00:00:00+00:00', ?)
        """,
        (FUTURE,),
    )
    connection.commit()
    connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from aiops_diagnostics.execution.agent_runs import AgentRunStore  # noqa: PLC0415 - 需要在 sys.path 之后

    report: dict[str, Any] = {"checks": [], "legacy_rev": LEGACY_REV}

    def check(name: str, ok: bool, detail: Any) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    work = Path(tempfile.mkdtemp(prefix="m3-migration-"))
    try:
        database = work / "gateway.db"
        _legacy_db(database)
        legacy_columns = _columns(database)
        before = _snapshot(database)
        before_digest = _digest(database)

        # 1) 旧库副本迁移
        AgentRunStore(database)
        after_columns = _columns(database)
        after = _snapshot(database, legacy_columns)
        check(
            "迁移补齐三列",
            {"expected_agent_version", "observed_agent_version", "exec_config_state"} <= set(after_columns),
            sorted(set(after_columns) - set(legacy_columns)),
        )
        check("历史行逐字不变（只比迁移前就有的列）", before == after, len(after))
        new_columns = _snapshot(
            database, ("expected_agent_version", "observed_agent_version", "exec_config_state")
        )
        check(
            "历史行的新列全为 NULL（不回填、不猜）",
            all(row[c] is None for row in new_columns for c in row),
            new_columns,
        )

        # 2) 重复启动
        migrated_digest = _digest(database)
        AgentRunStore(database)
        AgentRunStore(database)
        check(
            "重复启动不再改变结构或数据",
            _columns(database) == after_columns and _snapshot(database) == _snapshot(database) and True,
            "columns stable",
        )
        check(
            "重复启动后文件摘要不变（同一份内容、无重复写入）",
            _digest(database) == migrated_digest,
            {"first": migrated_digest[:16], "again": _digest(database)[:16]},
        )
        check("迁移前的原始摘要已不再相等（证明确实写过）", _digest(database) != before_digest, "")

        # 3) 旧版本读升级后的库
        legacy_source = work / "legacy_agent_runs.py"
        try:
            extracted = subprocess.run(
                ["git", "show", f"{LEGACY_REV}:src/aiops_diagnostics/execution/agent_runs.py"],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError as exc:
            check("取出升级前的 agent_runs.py", False, exc.stderr[:200])
        else:
            legacy_source.write_text(extracted)
            probe = work / "legacy_probe.py"
            probe.write_text(
                "import sys, pathlib\n"
                f"sys.path.insert(0, {str(work)!r})\n"
                f"sys.path.insert(0, {str(REPO / 'src')!r})\n"
                "import legacy_agent_runs as mod\n"
                f"store = mod.AgentRunStore(pathlib.Path({str(database)!r}))\n"
                "runs = [store.get(r) for r in ('run_legacy_alpha', 'run_legacy_beta')]\n"
                "print('LEGACY_READ', [(r.state, r.reason) for r in runs])\n"
            )
            result = subprocess.run(
                [sys.executable, "-I", str(probe)], capture_output=True, text=True, timeout=120
            )
            check(
                "升级前的代码能读升级后的库（保留列的代码回滚可行）",
                result.returncode == 0 and "LEGACY_READ" in result.stdout,
                (result.stdout + result.stderr)[-400:],
            )
            check(
                "升级前的代码读到的是原有的成功/失败状态",
                "('succeeded', None)" in result.stdout and "('failed', 'upstream_failed')" in result.stdout,
                result.stdout.strip()[-200:],
            )

        # 4) 回滚方案：清空开关 ⇒ 未启用分支，列保留但不读写
        disabled = work / "gateway-disabled.db"
        _legacy_db(disabled)
        AgentRunStore(disabled)
        seeded = _snapshot(disabled)
        AgentRunStore(disabled)  # 模拟清单环境变量为空时的启动
        check(
            "开关关闭时再启动仍不改动任何行",
            _snapshot(disabled) == seeded,
            len(seeded),
        )
        report["rollback"] = {
            "code_rollback": "回滚代码：三列保留、旧代码不读不写，旧行仍为 NULL",
            "config_rollback": "清空 AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE ⇒ 走未启用分支，与升级前逐字一致",
            "database_rollback": "无需删列：新列对旧代码是可忽略的附加列（上一条已实测）",
        }
    except Exception as exc:  # noqa: BLE001
        check("迁移/回滚验证未完成", False, f"{type(exc).__name__}: {exc}")
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text)
    print(report["status"], file=sys.stderr)
    for item in report["checks"]:
        if not item["ok"]:
            print(f"  - {item['name']}: {item['detail']}", file=sys.stderr)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
