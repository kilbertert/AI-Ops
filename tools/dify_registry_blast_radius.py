#!/usr/bin/env python3
"""打开运行时注册表之前，先量一次它的爆炸半径（#587 / #584）。

**这一票要回答的问题**：给 41 的 `env-41.toml` 加一行 `[[dify_apps]]`，会改变谁的答复？

注册表是 **fail-closed** 的（#584）：表里只要有**任何一行**，未登记的 `(租户, 入口)`
就**什么都不选**——不回退到最新已发布的 agent，也不落到零阶回答，而是
`retrieval_status="unavailable"`。所以"加一行"不是一个局部改动，它是**整台主机的一次
全局开关**。这个脚本把这个开关打开前后的选版结果**逐租户列出来**，用**真实选择代码**
复算，不是纸面推理。

被替换的只有一样：**库是副本**。选择逻辑、`AgentStore`、`DifyAppRegistry` 全是仓库里
那份真代码。副本用完即删，绝不碰生产库。

用法
----
先看计划（什么都不做，退出码 2 = 未取证）：

    PYTHONPATH=src python tools/dify_registry_blast_radius.py \
      --db /path/to/gateway.db --manifest ops/environments/env-41.toml

真正复算（在 `--db` 的**副本**上写那几行绑定）：

    ... --measure

退出码
------
0 = 复算完成（`--measure`）；1 = 有断言失败；2 = **未取证**（默认的 `--plan` 什么都不做，
或库不可读）——把"只列了计划"读成"量过了"正是本脚本要防的那种假陈述。

纪律
----
- 默认**只复制、只读**；`--db` 被复制到临时目录后才写入，原库只以 `mode=ro` 打开。
- 不打印凭据、不打印订单号；租户 id 是运营已登记在册的事实，照打。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

#: 与 `dify_app_registry.BUSINESS_ENTRIES` 同一组；这里写死是为了让本脚本能在
#: 没有网关依赖的环境里也解释得清自己在量什么。
ENTRIES = ("consumer", "operator")


def _copy_database(source: Path) -> Path:
    """A writable copy of the live database, plus its WAL sidecars.

    WAL + shm travel with the file: copying the main file alone can lose the
    most recent commits, and this script's whole point is to measure the state
    that is actually being served.
    """
    workdir = Path(tempfile.mkdtemp(prefix="aiops-registry-blast-"))
    workdir.chmod(0o700)
    target = workdir / source.name
    for suffix in ("", "-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))
    return target


def _declared_bindings(manifest_path: Path) -> dict[tuple[str, str], str]:
    """The `[[dify_apps]]` rows a manifest declares: `(tenant, entry) -> agent name`.

    Read through the repository's own loader so the script cannot disagree with
    the runtime about what a binding is — a manifest that fails to load is a
    failure to measure, not an empty declaration.
    """
    from aiops_diagnostics.agent_manifest import ManifestError, load_manifest

    try:
        manifest = load_manifest(manifest_path)
    except (ManifestError, OSError) as exc:
        raise SystemExit(f"清单读不了（未取证）：{exc}") from exc
    return {(binding.tenant_id, binding.business_entry): binding.agent_name for binding in manifest.dify_apps}


def _tenants_in_play(database: Path) -> list[str]:
    """Every tenant this host has an agent for, or has served on the QA route.

    The union, not one of the two: a tenant with an agent but no traffic still
    flips (it starts being routed), and a tenant with traffic but no agent flips
    too (its fallback answer becomes `unavailable`).
    """
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT tenant_id FROM agents
            UNION
            SELECT DISTINCT tenant_id FROM agent_run_metrics
                WHERE route_type = 'qa' AND tenant_id IS NOT NULL
            ORDER BY 1
            """
        ).fetchall()
    return [str(row[0]) for row in rows]


def _selection_name(selection, names: dict[str, str]) -> str:
    if selection is None:
        return "（什么都不选 ⇒ SOP 兜底）"
    return f"{names.get(selection.agent_id, selection.agent_id)}#v{selection.version_no}"


def _measure(database: Path, declared: dict[tuple[str, str], str], *, apply: bool) -> list[tuple]:
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.qa_rag import select_customer_agent

    store = AgentStore(database)
    registry = DifyAppRegistry(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        names = dict(connection.execute("SELECT agent_id, name FROM agents"))

    if apply:
        # The declaration names an app_id the runtime does not consult when
        # selecting an agent; a placeholder is honest here and keeps this script
        # from needing Dify credentials to answer a selection question.
        for (tenant_id, entry), agent_name in declared.items():
            registry.put(DifyAppBinding(tenant_id, entry, "dify-app-unused-by-selection", agent_name))

    configured = registry.is_configured()
    rows: list[tuple] = []
    for tenant_id in _tenants_in_play(database):
        for entry in ENTRIES:
            before = _selection_name(select_customer_agent(store, tenant_id), names)
            if configured:
                binding = registry.lookup(tenant_id, entry)
                if binding is None:
                    after = "（未登记 ⇒ unavailable）"
                else:
                    after = _selection_name(
                        select_customer_agent(store, tenant_id, agent_name=binding.agent_name), names
                    )
            else:
                after = before
            rows.append((tenant_id, entry, before, after, after != before))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量运行时注册表开关的爆炸半径")
    parser.add_argument("--db", required=True, help="网关 SQLite（只读打开；写入落在副本上）")
    parser.add_argument("--manifest", required=True, help="声明 [[dify_apps]] 的清单")
    parser.add_argument("--measure", action="store_true", help="真正复算（默认只列计划）")
    args = parser.parse_args(argv)

    source = Path(args.db).expanduser().resolve()
    if not source.is_file():
        print(f"库不存在（未取证）：{source}", file=sys.stderr)
        return 2

    declared = _declared_bindings(Path(args.manifest).expanduser().resolve())
    print(f"库：{source}")
    print(f"清单声明 {len(declared)} 行绑定：")
    for (tenant_id, entry), agent_name in sorted(declared.items()):
        print(f"    {tenant_id} / {entry} → {agent_name}")
    if not declared:
        print("    （清单未声明任何绑定 ⇒ 注册表保持未采用，开关不会被打开）")

    if not args.measure:
        print("\n计划模式：什么都没做。加 --measure 在**副本**上真正复算（退出码 2 = 未取证）。")
        return 2

    database = _copy_database(source)
    try:
        rows = _measure(database, declared, apply=True)
    finally:
        shutil.rmtree(database.parent, ignore_errors=True)

    changed = [row for row in rows if row[4]]
    print(f"\n{'租户':21} {'入口':9} {'注册表关（现状）':26} {'注册表开（复算）':26}")
    for tenant_id, entry, before, after, flipped in rows:
        print(f"{tenant_id:21} {entry:9} {before:26} {after:26}{'  ← 变了' if flipped else ''}")
    print(f"\n{len(changed)} / {len(rows)} 个 (租户, 入口) 组合的选版结果会变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
