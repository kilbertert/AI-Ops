"""运维分析作业在**运行时**这一层的两个终态性质（#700 在生产上逮到的两处）。

本文件与 ``test_ops_analysis.py`` 的分工：那一份测装配与聚合（纯函数、无作业），
这一份测**作业**——它必须要么给出契约，要么给出一个终态行。第一版交付时它能两者都不给：
叙述轮回了散文 ⇒ ``AgentRuntimeError`` ⇒ 作业停在 ``running`` 直到过期。

两条判据：

* 叙述轮出错时作业**终结**为 ``failed`` / ``ANALYSIS_FAILED``（不是停在 ``running``）；
* 叙述轮拿到的 ``output_schema`` 就是本模块声明的那个（不是 ``None``）。

第二条看着像实现细节，其实是第一条的**上游**：schema 从未传给那一轮，模型才有机会回散文。
只断言第一条会得到一个"靠兜底兜住"的实现；两条一起才要求把 schema 真的接上。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from aiops_diagnostics.codex_runtime import AgentRuntimeError, CodexTurnOutput
from aiops_diagnostics.config import Settings
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TENANT = "T-1"
WINDOW = ("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")
SITE = "S-1"


class _FakeCursor:
    def execute(self, sql: str, params: Any = None) -> None:
        del sql, params

    def fetchone(self) -> dict[str, Any]:
        return {"total": 10, "abnormal_ended": 2, "normal_ended": 8}

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"site_id": SITE, "abnormal_ended": 2}]


class _FakeMySql:
    """真实聚合被换掉；本文件关心的是**作业**怎么终结，不是数字怎么算。"""

    def count_devices(self, sites: tuple[str, ...]) -> int:
        del sites
        return 1

    def aggregate_order_statuses(self, sites: tuple[str, ...], start: str, end: str) -> dict[str, Any]:
        del sites, start, end
        return {"total": 10, "abnormal_ended": 2, "normal_ended": 8, "top_sites": ((SITE, 2),)}


class _FakeSources:
    mysql = _FakeMySql()


def _scope() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _runtime(tmp_path: Path, monkeypatch) -> tuple[GatewayRuntime, GatewayStore]:
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.scoped_live_sources",
        lambda settings, *, scope: _sources(settings, scope),
    )
    return GatewayRuntime(store, gateway_settings, settings), store


class _sources:
    def __init__(self, settings: Any, scope: QueryScope) -> None:
        del settings, scope

    def __enter__(self) -> _FakeSources:
        return _FakeSources()

    def __exit__(self, *exc: Any) -> None:
        return None


def _run_analysis(runtime: GatewayRuntime, store: GatewayStore) -> dict[str, Any]:
    created = runtime.start_operations_analysis(
        _scope(),
        site_ids=(SITE,),
        window_start=WINDOW[0],
        window_end=WINDOW[1],
        language="zh",
    )
    deadline = time.monotonic() + 5
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline, current
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)
    return current


def test_a_prose_answer_ends_the_analysis_job_instead_of_leaving_it_running(
    tmp_path: Path, monkeypatch
) -> None:
    """模型回了散文 ⇒ 作业终结为 failed，而不是永远 ``running``。

    这是 41 上第一版交付的真实形状：叙述轮返回了 ``main_cause：…`` 这样的散文，解析
    拿不到 JSON 就 ``raise AgentRuntimeError``，而那条 ``except`` 只接了
    ``SourceError`` / ``ValueError`` —— 于是**没有任何终态写入**，轮询方看到的是一个
    永远 ``running`` 的作业，直到它过期。
    """

    def prose(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AgentRuntimeError("operations-analysis narrative returned invalid JSON")

    runtime, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("aiops_diagnostics.ops_analysis_narrative.run_narrative_turn", prose)
    try:
        job = _run_analysis(runtime, store)
    finally:
        runtime.shutdown()

    assert job["status"] == "failed", job
    assert job["error_code"] == "ANALYSIS_FAILED", job


def test_the_narrative_turn_is_given_the_declared_output_schema() -> None:
    """叙述轮的 ``output_schema`` 是那个"只有两段文字"的结构，不是 ``None``。

    这条是上一条的上游：schema 没传，模型自然按散文回答；只靠"出错也要终结"兜底，
    得到的是一个**每次都失败**的卡。两处一起才构成"要么答对，要么终结"。
    """
    from aiops_diagnostics.ops_analysis_narrative import OPS_NARRATIVE_SCHEMA, run_narrative_turn

    seen: dict[str, Any] = {}

    class _Session:
        def run(self, prompt: str, *, output_schema: dict[str, Any] | None = None) -> CodexTurnOutput:
            seen["schema"] = output_schema
            return CodexTurnOutput(
                turn_id="t-1",
                final_response='{"main_cause": "设备断连", "recommendation": "核对上报"}',
                usage={},
            )

        def close(self) -> None:
            pass

    payload = run_narrative_turn(
        {"failure_rate": 0.2, "risk": "abnormal"},
        Settings().agent,
        session=_Session(),  # type: ignore[arg-type]
    )

    assert seen["schema"] == OPS_NARRATIVE_SCHEMA, seen["schema"]
    assert seen["schema"]["required"] == ["main_cause", "recommendation"]
    assert payload == {"main_cause": "设备断连", "recommendation": "核对上报"}


def test_the_narrative_workspace_asks_for_the_narrative_shape_not_the_qa_one(
    tmp_path: Path, monkeypatch
) -> None:
    """护栏文本要与这一轮的提示词说同一个答案形状。

    这是 41 上第二处失败的直接成因：零阶工作区的 AGENTS.md 写着「回答问题、末尾附提醒语、
    返回 ``text``」，而叙述轮的提示词要 ``{main_cause, recommendation}`` —— 模型照护栏做，
    回了一个 ``{"text": …}`` 信封，叙述轮随即拒绝它。**同一轮里两条互相矛盾的指令**，
    正是这个模块的文档字符串已经记过一次的失败形态。
    """
    from aiops_diagnostics.agent_workspace import NARRATIVE_ANSWER_CONTRACT, AgentWorkspace
    from aiops_diagnostics.ops_analysis_narrative import run_narrative_turn

    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    captured: dict[str, Any] = {}

    class _Session:
        def __init__(self, workspace: Any, *_args: Any, **_kwargs: Any) -> None:
            captured["contract"] = (workspace.path / "AGENTS.md").read_text(encoding="utf-8")

        def run(self, prompt: str, *, output_schema: dict[str, Any] | None = None) -> CodexTurnOutput:
            del prompt, output_schema
            return CodexTurnOutput(
                turn_id="t-1",
                final_response='{"main_cause": "设备断连", "recommendation": "核对上报"}',
                usage={},
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("aiops_diagnostics.ops_analysis_narrative.SDKCodexSession", _Session)
    run_narrative_turn(
        {"failure_rate": 0.2},
        settings.agent,
        project_root=tmp_path,
        session=None,  # type: ignore[arg-type]
    )

    contract = captured["contract"]
    assert "`main_cause` and `recommendation`" in contract
    assert "`text` answer" not in contract, "护栏仍在要求零阶那份 text 信封"
    # ``create_qa`` 的默认契约仍是零阶那份 —— 这一轮只是显式换了形状。
    default = (
        AgentWorkspace.create_qa(
            tmp_path,
            tmp_path / "runs2",
            language="zh",
        ).path
        / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "`text` answer" in default
    assert NARRATIVE_ANSWER_CONTRACT == "narrative"


def test_the_analysis_route_type_is_a_recordable_metric(tmp_path: Path) -> None:
    """``operations_analysis`` 必须是度量层认识的 ``route_type``。

    分析线把 ``route_type="operations_analysis"`` 交给 ``_record_metric``，而
    ``MetricsStore.ROUTE_TYPES`` 里没有它 —— 于是 ``record`` 抛
    ``MetricsValidationError``，被 ``_record_metric`` 的 ``contextlib.suppress`` 吞掉：
    **分析线一条指标都记不下来，且静默**。抑制本身是对的（度量不该让运行失败），
    所以这个缺陷不会有任何症状 —— 只有把这一条判据钉住才能发现它。
    """
    from aiops_diagnostics.metrics_store import ROUTE_TYPES, MetricsStore

    assert "operations_analysis" in ROUTE_TYPES
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id=TENANT, route_type="operations_analysis", outcome="completed")
    assert [row["route_type"] for row in metrics.list_runs(TENANT, limit=5)] == ["operations_analysis"]
