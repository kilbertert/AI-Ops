"""`tools/dify_registry_blast_radius.py` must measure a SWITCH, not a change (#587).

The registry is fail-closed (#584), so declaring one `[[dify_apps]]` row turns the
whole host's selection rule over: every unregistered `(tenant, entry)` stops being
served by the pre-registry "newest published agent" default. A measurement that
only looked at the declared pair would report "nothing changed" for the one tenant
the operator was thinking about, and miss every other tenant on the host.

These tests drive the tool's real selection code (``AgentStore`` +
``select_customer_agent`` + ``DifyAppRegistry``) over a real SQLite file, and
assert the three claims the tool makes:

* the declared pair keeps the agent the old rule already chose;
* an undeclared entry of the same tenant flips;
* a tenant with no agent at all flips from "SOP fallback" to "nothing selected".

They also pin the two disciplines the tool's exit codes promise: the default run
writes nothing anywhere (the source database is byte-identical afterwards), and
"we only listed a plan" is exit 2, not 0.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.metrics_store import MetricsStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

ROOT = Path(__file__).parents[1]

#: A tenant that has a published agent, and one that has only ever had traffic.
TENANT_WITH_AGENT = "T-A"
TENANT_WITHOUT_AGENT = "T-B"


def _load():
    """Import the tool by path — `tools/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "dify_registry_blast_radius", ROOT / "tools" / "dify_registry_blast_radius.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load()


class _PublishOk:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _admin_context(tenant_id: str) -> ScopeContext:
    subject = SubjectRecord(b_user_id=f"B-{tenant_id}", tenant_id=tenant_id)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant_id,
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _publish_customer(store_path: Path, tenant_id: str, name: str) -> None:
    manager = AgentManager(
        AgentStore(store_path), knowledge_resolver=_PublishOk(), allowed_models=("aiops-api",)
    )
    context = _admin_context(tenant_id)
    created = manager.create(
        context,
        name=name,
        description="",
        config=AgentConfig(
            agent_type="customer",
            prompt=f"提示词:{name}",
            knowledge_base_ids=("kb-a",),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    manager.publish(context, created.agent_id, expected_revision=created.revision)


def _live_looking_database(tmp_path: Path) -> Path:
    """A gateway database with one published agent and QA traffic for two tenants."""
    path = tmp_path / "gateway.db"
    _publish_customer(path, TENANT_WITH_AGENT, "客服甲")
    metrics = MetricsStore(path)
    for tenant_id in (TENANT_WITH_AGENT, TENANT_WITHOUT_AGENT):
        metrics.record(tenant_id=tenant_id, route_type="qa", outcome="completed")
    return path


def _manifest(tmp_path: Path, *, rows: str) -> Path:
    path = tmp_path / "env.toml"
    path.write_text(
        f"""
[[agents]]
tenant_id = "{TENANT_WITH_AGENT}"
name = "客服甲"
agent_type = "customer"
prompt = "你是客服"
knowledge_base_ids = ["kb-a"]
model = "aiops-api"
{rows}
""",
        encoding="utf-8",
    )
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_declaring_one_row_flips_the_switch_for_the_whole_host(tool, tmp_path: Path, capsys) -> None:
    """The measurement #587 needs: one row, and the blast radius is the host.

    Declared pair keeps the agent the old rule chose; the SAME tenant's other
    entry stops being served; and a tenant with no agent loses its fallback
    answer entirely. Any assertion missing here would let a "局部改动" reading
    stand.
    """
    database = _live_looking_database(tmp_path)
    manifest = _manifest(
        tmp_path,
        rows=f'\n[[dify_apps]]\ntenant_id = "{TENANT_WITH_AGENT}"\n'
        'business_entry = "consumer"\napp_id = "dify-app-1"\nagent_name = "客服甲"\n',
    )
    before = _sha(database)

    assert tool.main(["--db", str(database), "--manifest", str(manifest), "--measure"]) == 0
    out = capsys.readouterr().out

    rows = {
        (line.split()[0], line.split()[1]): line
        for line in out.splitlines()
        if line.startswith(TENANT_WITH_AGENT) or line.startswith(TENANT_WITHOUT_AGENT)
    }
    declared = rows[(TENANT_WITH_AGENT, "consumer")]
    undeclared_entry = rows[(TENANT_WITH_AGENT, "operator")]
    agentless = rows[(TENANT_WITHOUT_AGENT, "consumer")]

    assert "客服甲#v1" in declared and "← 变了" not in declared, (
        "已登记的那一对应当逐字不变 —— 否则这不是「加一行」，是「换回答者」"
    )
    assert "← 变了" in undeclared_entry and "unavailable" in undeclared_entry, (
        "同一租户的另一个入口会从「有新 agent 就用」变成「未登记」"
    )
    assert "← 变了" in agentless and "unavailable" in agentless, (
        "无 agent 的租户会从 SOP 兜底回答变成什么都不选 —— 这是开关最容易被漏掉的一半"
    )
    assert _sha(database) == before, "原件一个字节都不能变：写入必须落在副本上"


def test_the_registry_stays_unadopted_when_the_manifest_declares_nothing(
    tool, tmp_path: Path, capsys
) -> None:
    """An empty `[[dify_apps]]` is the inert case #584 shipped for: zero flips."""
    database = _live_looking_database(tmp_path)
    manifest = _manifest(tmp_path, rows="")

    assert tool.main(["--db", str(database), "--manifest", str(manifest), "--measure"]) == 0
    out = capsys.readouterr().out
    assert "0 / 4 个 (租户, 入口) 组合的选版结果会变" in out


def test_plan_mode_reports_untaken_and_writes_nothing(tool, tmp_path: Path, capsys) -> None:
    """Listing a plan is not taking evidence — exit 2, and nothing on disk changes."""
    database = _live_looking_database(tmp_path)
    manifest = _manifest(tmp_path, rows="")
    before = _sha(database)

    assert tool.main(["--db", str(database), "--manifest", str(manifest)]) == 2
    assert "计划模式" in capsys.readouterr().out
    assert _sha(database) == before


def test_a_missing_database_is_untaken_not_an_exception(tool, tmp_path: Path, capsys) -> None:
    manifest = _manifest(tmp_path, rows="")
    assert tool.main(["--db", str(tmp_path / "nope.db"), "--manifest", str(manifest)]) == 2
    assert "未取证" in capsys.readouterr().err
