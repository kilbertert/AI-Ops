#!/usr/bin/env python3
"""#587 判据二：**真实租户经显式注册表映射被正确路由；未登记的仍然 fail closed**。

为什么必须在**生产数据的副本**上做：判据说的是「真实租户」。在 41 上，两个候选
（旧规则「最新已发布的客服 agent」与注册表**声明**的那个）**恰好是同一个** —— 于是
"打开注册表、读到的还是它"在两个实现下都成立，**不可区分**，那就不是证据。

做法：复制生产 `gateway.db`（含 WAL/shm 边车）到临时目录，在副本里

1. 给每个有已发布客服 agent 的真实租户各**新发布一个更晚的对照 agent** —— 让
   「旧规则（最新）」与「注册表（声明）」**指向不同的 agent**；
2. 走**运行时那道门本身**（`GatewayRuntime._registered_agent`，不是复述它的条件）
   读每个真实 `(租户, 入口)`，断言：
   * 登记了 ⇒ 门给出**声明的名字**，且 `select_customer_agent(..., agent_name=声明)` 选中的
     正是那一个（**不是**更晚的对照 agent）；
   * 没登记且门已启用 ⇒ 门给出 `(None, configured=True)` —— 运行时据此返回 `unavailable`，
     既不回退到"最新已发布"，也不落到零阶回答。

真的：生产库里的真实行、真实的 `AgentStore` / `select_customer_agent` /
`DifyAppRegistry` / `GatewayRuntime._registered_agent`、真实租户与 agent 名。
假的：库是**副本**（这正是它安全、且能凭空造"更晚的 agent"的原因）。

退出码 0 = 可区分且两条判据都成立；1 = 断言失败（含"不可区分"）；2 = **未取证**。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DEFAULT_DB = "/var/lib/aiops-41/gateway/gateway.db"
ENTRIES = ("consumer", "operator")
PROBE_TENANT = "probe-tenant"  # 只用在副本里，形状须过 tenant_id 校验


def _copy_database(source: Path) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="aiops-registry-route-"))
    workdir.chmod(0o700)
    target = workdir / source.name
    for suffix in ("", "-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))
    return target


def _customer_agents_with_kb(database: Path) -> dict[str, tuple[str, str]]:
    """`tenant → (agent 名, 一个知识库 id)`：最新发布的、带知识库的客服 agent。

    「是不是客服 agent」按运行时同一条口径判：已发布 + 带知识库 + 不是宣传钉选。
    宣传 agent 被排除，是因为宣传路由跑在注册表那道门**之外**（它由快捷动作钉住，
    不是默认映射）。
    """
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT tenant_id, name, config_json FROM agents "
            "WHERE status='published' AND published_version IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
    newest: dict[str, tuple[str, str]] = {}
    for tenant_id, name, config_json in rows:
        knowledge = json.loads(config_json).get("knowledge_base_ids") or []
        if not knowledge:
            continue
        newest.setdefault(tenant_id, (name, knowledge[0]))
    return newest


def _promo_pinned_ids(database: Path, tenant_id: str) -> frozenset[str]:
    """被已发布快捷动作钉为宣传目标的 agent id。

    直接**复用运行时那个函数**（`qa_rag._pinned_promo_agents`）而不是自己抄一遍：
    这里的用途是"把观察不到的候选排除掉"，两份实现一旦漂移，排除的集合就会与运行时不同，
    于是脚本会开始对**运行时根本不会选**的 agent 做断言。宣传 agent 之所以要排除，是因为
    它**永远**不会被 `select_customer_agent` 选中（那条路由在注册表这道门**之外**、
    由快捷动作钉住），在副本里给它写登记是**合法配置**却观察不到。

    私有名在此处是刻意复用：这个脚本与本仓同一个进程里跑，而抄一份的代价是漂移。
    """
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.qa_rag import _pinned_promo_agents

    return _pinned_promo_agents(AgentStore(database), tenant_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#587 判据二：注册表路由（生产库副本上）")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--config", default="/etc/aiops-41/production.env")
    parser.add_argument("--probe-agent", default="canary-registry-proof")
    args = parser.parse_args(argv)

    source = Path(args.db).expanduser().resolve()
    if not source.is_file():
        print(f"未取证：库不存在 {source}", file=sys.stderr)
        return 2

    database = _copy_database(source)
    print(f"副本：{database}")

    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.qa_rag import select_customer_agent

    class _PublishOk:
        def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
            del tenant_id, knowledge_base_ids

    candidates = _customer_agents_with_kb(database)
    declared: dict[str, tuple[str, str]] = {}
    for tenant, (name, kb_id) in candidates.items():
        pinned = _promo_pinned_ids(database, tenant)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            agent_id = connection.execute(
                "SELECT agent_id FROM agents WHERE tenant_id=? AND name=?", (tenant, name)
            ).fetchone()[0]
        if agent_id in pinned:
            print(f"    跳过 {tenant}：{name} 被快捷动作钉为宣传目标 ⇒ 永远不被客服路由选中")
            continue
        declared[tenant] = (name, kb_id)
    if not declared:
        print("未取证：生产数据里没有带知识库的已发布客服 agent", file=sys.stderr)
        shutil.rmtree(database.parent, ignore_errors=True)
        return 2
    print(f"生产数据里有客服 agent 的租户：{sorted(declared)}")

    # ── 制造"可区分"：每个这样的租户在副本里各多一个**更晚**的客服 agent ──────
    manager = AgentManager(
        AgentStore(database), knowledge_resolver=_PublishOk(), allowed_models=("deepseek-v4-flash",)
    )
    for tenant_id, (_name, kb_id) in declared.items():
        context = admin_context(tenant_id)
        created = manager.create(
            context,
            name=args.probe_agent,
            description="注册表判据的可区分对照（仅副本）",
            config=AgentConfig(
                agent_type="customer",
                prompt="对照：本条不应被选中。",
                knowledge_base_ids=(kb_id,),
                model="deepseek-v4-flash",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(context, created.agent_id, expected_revision=created.revision)
    print(f"副本里每个租户各多了一个更晚的 {args.probe_agent} ⇒ 旧规则会选它，注册表不该选它")

    store = AgentStore(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        names = dict(connection.execute("SELECT agent_id, name FROM agents"))
        tenants = [row[0] for row in connection.execute("SELECT DISTINCT tenant_id FROM agents ORDER BY 1")]

    registry = DifyAppRegistry(database)
    runtime = GatewayRuntime(
        GatewayStore(database), GatewayServerSettings.from_env(), Settings.from_config(Path(args.config))
    )

    def rule(tenant_id: str) -> str:
        selection = select_customer_agent(store, tenant_id)
        if selection is None:
            return "（什么都不选）"
        return f"{names[selection.agent_id]}#v{selection.version_no}"

    def via_registry(tenant_id: str, declared_name: str) -> str:
        selection = select_customer_agent(store, tenant_id, agent_name=declared_name)
        if selection is None:
            return "（什么都不选）"
        return f"{names[selection.agent_id]}#v{selection.version_no}"

    failures: list[str] = []

    # ── 判据 1（先做，此时门还没启用）：未登记时保持旧规则，逐字不变 ──────────
    print("\n【判据 1a】注册表**未启用** ⇒ 保持注册表出现之前的选法（可安全惰性上线）")
    for tenant_id in tenants:
        name, configured = runtime._registered_agent(tenant_id, "consumer")
        assert (name, configured) == (None, False), (tenant_id, name, configured)
    print("    所有租户的 `_registered_agent` 都返回 (None, False) ⇒ 门未启用 ✓")

    # ── 启用门：先写一行**没人会用到**的登记，让 `is_configured()` 为真 ─────────
    registry.put(DifyAppBinding(PROBE_TENANT, "consumer", "probe-app", args.probe_agent))
    print(f"\n写入一行探针登记后，门已启用：{registry.is_configured()}")

    # ── 判据 1b：启用后，未登记的 (租户, 入口) ⇒ 门给出 (None, True) ───────────
    print("\n【判据 1b】注册表**已启用**、这一对**未登记** ⇒ 运行时返回 unavailable")
    for tenant_id in tenants:
        for entry in ENTRIES:
            name, configured = runtime._registered_agent(tenant_id, entry)
            if not configured or name is not None:
                failures.append(f"{tenant_id}/{entry}: 门给出 ({name!r}, {configured})，应为 (None, True)")
            else:
                print(f"    {tenant_id} / {entry} → (None, True) ⇒ unavailable ✓")

    # ── 判据 2：登记 ⇒ 由**声明**的那一个服务，且与"最新"可区分 ──────────────
    print("\n【判据 2】登记 ⇒ 由声明的那一个服务（而不是「最新已发布」）")
    for tenant_id, (agent_name, _kb) in sorted(declared.items()):
        registry.put(DifyAppBinding(tenant_id, "consumer", "dify-app-unused-by-selection", agent_name))
        name, configured = runtime._registered_agent(tenant_id, "consumer")
        served = via_registry(tenant_id, name or "")
        old = rule(tenant_id)
        distinguishable = served != old
        print(
            f"    {tenant_id}：门给 {name!r}；旧规则选 {old}；按门选 {served}"
            f"{'（可区分 ✓）' if distinguishable else '（**不可区分** —— 这次证明不了注册表起了作用）'}"
        )
        if name != agent_name:
            failures.append(f"{tenant_id}: 门给出 {name!r}，声明的是 {agent_name!r}")
        if served.split("#")[0] != agent_name:
            failures.append(f"{tenant_id}: 按门选出的不是 {agent_name!r}（得到 {served}）")
        if not distinguishable:
            failures.append(f"{tenant_id}: 新旧两条路指向同一个 agent ⇒ 不可区分")

    shutil.rmtree(database.parent, ignore_errors=True)
    if failures:
        print("\n断言失败：", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("\n⇒ 真实租户经显式注册表映射被正确路由；未登记的仍 fail closed。**可区分，成立**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
