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

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.codex_runtime import AgentRuntimeError, CodexTurnOutput
from aiops_diagnostics.config import Settings
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.ops_analysis_narrative import build_narrative_prompt
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TENANT = "T-1"
WINDOW = ("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")
SITE = "S-1"

#: What the aggregate was called with, per test. Reset in ``_runtime``.
_SEEN_SITES: list[tuple[str, ...] | None] = []


class _FakeCursor:
    def execute(self, sql: str, params: Any = None) -> None:
        del sql, params

    def fetchone(self) -> dict[str, Any]:
        return {"total": 10, "abnormal_ended": 2, "normal_ended": 8}

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"site_id": SITE, "abnormal_ended": 2}]


class _FakeMySql:
    """真实聚合被换掉；本文件关心的是**作业**怎么终结，不是数字怎么算。

    每一张卡都给**非空**结果：空结果走的是「确定性答案」那条分支，根本不碰模型，
    也就照不到本文件要照的那些路径。#700 踩到的正是这个差别 —— 本地替身一直给空数据，
    所以那条有数据才走到的分支从来没被跑到。
    """

    def count_devices(self, sites: tuple[str, ...] | None) -> int:
        del sites
        return 1

    def aggregate_order_statuses(self, sites: tuple[str, ...] | None, start: str, end: str) -> dict[str, Any]:
        #: Recorded, not `del`'d: "did the aggregate get asked to run at all, and
        #: with what site narrowing" is exactly what the whole-tenant regression
        #: is about — a card that never reaches this line still terminates, just
        #: with the wrong answer.
        del start, end
        _SEEN_SITES.append(sites)
        return {"total": 10, "abnormal_ended": 2, "normal_ended": 8, "top_sites": ((SITE, 2),)}

    def aggregate_offline_incidents(self, sites: tuple[str, ...], start: str, end: str) -> dict[str, Any]:
        del sites, start, end
        return {"incidents": 3, "offline_devices": 2, "open_incidents": 1, "offline_minutes": 42}

    def aggregate_fault_codes(self, sites: tuple[str, ...], start: str, end: str) -> dict[str, Any]:
        del sites, start, end
        return {"total_faults": 9, "faulty_devices": 2, "top_codes": (("NoError", 5), ("OtherError", 4))}

    def aggregate_payment_split(self, sites: tuple[str, ...], start: str, end: str) -> dict[str, Any]:
        del sites, start, end
        return {"total": 10, "paid": 8, "paid_refunded": 2, "paid_net": 6, "unpaid": 2}


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


def _tenant_wide_scope() -> ScopeContext:
    """A caller whose range is the **whole tenant** — ``resolve_query_scope`` gives
    it ``site_ids=None``. This is the 管家 account the surface exists for, and the
    one the review of #721 proved never got an answer.
    """
    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="all"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _scope_error(site_ids: tuple[str, ...]):
    """The real ``ScopeError`` the resolver raises for an oversized data scope."""
    from aiops_diagnostics.query_scope import MAX_SCOPE_IDS, SCOPE_ERROR_SCOPE_TOO_LARGE, ScopeError

    assert len(site_ids) > MAX_SCOPE_IDS
    return ScopeError(f"数据范围 ID 数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)


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
    _SEEN_SITES.clear()
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


@pytest.mark.parametrize("card", ["offline", "fault_code", "payment"])
def test_every_card_with_data_reaches_a_terminal_row(tmp_path: Path, monkeypatch, card: str) -> None:
    """**有数据的**三张卡也必须到终态（不是停在 ``running``）。

    #700 部署后跑验收时才逮到：``fault_code`` 那一路的 ``_narrative_for`` 调用点少传了
    ``card`` 参数（``_narrative_for`` 在 #698 加了这个参数，调用点没跟上），于是**只要有
    故障码数据**就在 worker 里抛 ``TypeError`` —— 没有任何 handler 接住它，作业停在
    ``running`` 直到过期。

    这条判据此前不存在的原因正是本文件第一版的问题：替身只回**空**聚合，空结果走
    「确定性答案」分支、根本不碰模型，所以那条有数据才走到的路一次都没被跑到。
    修法不只是补上那个参数（见 ``_compute_card``），还包括让这个替身**回非空数据**——
    否则同一个形状下次还会以另一种面貌回来。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.run_narrative_turn",
        lambda *a, **k: {"main_cause": "成因", "recommendation": "建议"},
    )
    try:
        created = runtime.start_operations_card(
            _scope(),
            card=card,
            site_ids=(SITE,),
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in {"completed", "failed", "inconclusive"}:
            assert time.monotonic() < deadline, (card, current)
            time.sleep(0.01)
            current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)
    finally:
        runtime.shutdown()

    assert current["status"] == "completed", (card, current)
    result = current.get("result") or {}
    assert result.get("main_cause") == "成因", (card, result)
    # The card's own numbers, by the fields its contract actually uses, and by
    # **the values the fake returned** — not merely "something is not None".
    # Asserting the values is what catches an aggregate that silently comes back
    # empty: a review pointed out that `is not None` (never mind `> 0`) would
    # pass on the very regression this file exists to prevent.
    expected = {
        "offline": {"incidents": 3, "offline_devices": 2, "open_incidents": 1, "offline_minutes": 42},
        "fault_code": {"total_faults": 9, "faulty_devices": 2},
        "payment": {"total": 10, "paid": 8, "paid_refunded": 2, "paid_net": 6, "unpaid": 2},
    }[card]
    for key, value in expected.items():
        assert result.get(key) == value, (card, key, result.get(key), result)
    if card == "fault_code":
        assert result.get("top_codes") == [
            {"error_code": "NoError", "fault_count": 5},
            {"error_code": "OtherError", "fault_count": 4},
        ], result.get("top_codes")


def test_the_narrative_prompt_carries_each_card_own_numbers() -> None:
    """提示词必须把**这张卡自己的**数字交给模型。

    这是评审在 #710 上逮到的：``build_narrative_prompt`` 里的键名写死成失败率那一套，
    于是首次真正走到有数据的故障码卡时，模型只拿到一个 risk 标签与时间窗 ——
    **没有故障数、没有故障码排名**，而它仍被要求写「先查哪个码」。离线卡与支付卡同理
    （分别缺 incidents/offline_minutes 与 four buckets）。

    判据落在**真正发出去的那段提示词**上：键改名、契约加字段、某张卡少传一个数，
    都会在这里显形。断言到值，不是「含有某个词」。
    """
    from aiops_diagnostics.ops_analysis import (
        FaultCodeAggregate,
        OfflineAggregate,
        PaymentAggregate,
        build_fault_code_contract,
        build_offline_contract,
        build_payment_contract,
    )

    window = {"language": "zh", "site_ids": ("S-1",)}
    cases = {
        "fault_code": (
            build_fault_code_contract(
                FaultCodeAggregate(
                    total_faults=9,
                    faulty_devices=2,
                    top_codes=(("NoError", 5), ("OtherError", 4)),
                    devices_total=7,
                ),
                None,
                **window,
            ),
            ("total_faults: 9", "faulty_devices: 2", "NoError", "OtherError"),
        ),
        "offline": (
            build_offline_contract(
                OfflineAggregate(
                    incidents=3, offline_devices=2, open_incidents=1, offline_minutes=42, devices_total=7
                ),
                None,
                **window,
            ),
            ("incidents: 3", "offline_devices: 2", "offline_minutes: 42", "open_incidents: 1"),
        ),
        "payment": (
            build_payment_contract(
                PaymentAggregate(total=10, paid=8, paid_refunded=2, paid_net=6, unpaid=2),
                None,
                **window,
            ),
            ("total: 10", "paid: 8", "paid_refunded: 2", "paid_net: 6", "unpaid: 2"),
        ),
    }
    for card, (facts, wanted) in cases.items():
        prompt = build_narrative_prompt(facts, language="zh")
        for fragment in wanted:
            assert fragment in prompt, (card, fragment, prompt)
        # The failure-rate keys it used to hard-code are not what grounds these.
        assert "counts:" not in prompt, (card, prompt)


def test_a_locked_database_ends_the_job_instead_of_leaving_it_running(tmp_path: Path, monkeypatch) -> None:
    """**收尾写本身失败**时，作业也必须终结。

    第 41 上部署后的验收里有一张故障码卡停在 ``running`` 直到过期，而进程里
    **没有任何 traceback / 日志 / 指标**：叙述轮已经跑完，失败发生在**收尾写**上
    （``sqlite3.OperationalError: database is locked``），而那段写在 worker 的
    ``except`` **之外** —— 枚举异常类的写法天然覆盖不到它。

    判据不依赖锁从哪来：把收尾写换成抛 ``OperationalError``，作业仍必须离开 ``running``。
    至于它落在 ``failed`` 还是短暂 ``running``（因为兜底那一笔也可能撞同一把锁），
    由下面两条断言分开表达——**重要的性质是「不会永远挂着」**，而写成功时它必须是
    ``failed`` 而不是 ``completed``（那会是一句假陈述）。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.run_narrative_turn",
        lambda *a, **k: {"main_cause": "成因", "recommendation": "建议"},
    )
    real_end = GatewayRuntime._end_analysis_job
    seen: list[dict[str, Any]] = []

    def recording(self: GatewayRuntime, analysis_id: str, tenant_id: str, **kwargs: Any) -> None:
        seen.append({"result": kwargs.get("result"), "error_code": kwargs.get("error_code")})
        return real_end(self, analysis_id, tenant_id, **kwargs)

    monkeypatch.setattr(GatewayRuntime, "_end_analysis_job", recording)
    real_update = GatewayStore.update_standard_diagnosis
    fail_once = {"done": False}

    def flaky(self: GatewayStore, diagnosis_id: str, **kwargs: Any) -> bool:
        # The ``running`` claim succeeds; the *first* terminal write meets the lock.
        if kwargs.get("status") in ("completed", "failed") and not fail_once["done"]:
            fail_once["done"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_update(self, diagnosis_id, **kwargs)

    monkeypatch.setattr(GatewayStore, "update_standard_diagnosis", flaky)
    try:
        created = runtime.start_operations_card(
            _scope(),
            card="fault_code",
            site_ids=(SITE,),
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        aid = created["diagnosis_id"]
        deadline = time.monotonic() + 5
        status = "queued"
        while time.monotonic() < deadline:
            time.sleep(0.01)
            with sqlite3.connect(store.path) as connection:
                row = connection.execute(
                    "SELECT status FROM standard_diagnoses WHERE diagnosis_id = ?", (aid,)
                ).fetchone()
            status = row[0] if row else "missing"
            if status in {"completed", "failed", "inconclusive"}:
                break
    finally:
        runtime.shutdown()

    # The helper was reached at all — i.e. the terminal path no longer dies in place.
    assert seen, "收尾写没有走到兜底入口"
    # And the job is no longer ``running``: the lock is transient, so the
    # helper's own retry lands it. Never ``completed`` — that would claim a
    # result it could not store.
    assert status == "failed", status
    assert seen[0]["error_code"] is None, "第一次收尾写是「成功」那一条，锁是在它内部抛的"


def test_a_tool_request_turn_is_not_a_narrative(tmp_path: Path, monkeypatch) -> None:
    """工具请求回合**不是**叙述 —— 作业必须落 ``failed``，不是静默的 ``completed``。

    #700 在 41 上逮到：支付卡的叙述轮回了 ``{"kind": "tool_requests", …}``
    （那一轮的工具调用全部因沙箱不可用失败，模型转而请求订单快照）。``_parse_narrative``
    当时**只要解析得出一个 dict 就收下**，于是 ``_narrative`` 按名字取两个键都取不到
    ⇒ 两个散文字段都是 ``None``，而作业以 **``completed``** 落库、**没有原因码**。

    一张说 ``completed`` 却没有 ``main_cause`` 的卡，读起来像「聚合本身没话说」——
    那句假陈述正是 ``_narrative_for`` 的文档字符串里已经写过的、必须要失败的那种情形。
    判据落在**作业终态**上，不是落在某个函数返回什么。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)

    # The seam is the **parser**, not the turn: stub the session so the real
    # ``run_narrative_turn`` reads a real model answer through the real parser.
    class _Session:
        def run(self, prompt: str, *, output_schema: Any = None) -> CodexTurnOutput:
            del prompt, output_schema
            return CodexTurnOutput(
                turn_id="t-1",
                final_response='{"kind": "tool_requests", "tool_requests": [{"tool": "order_snapshot"}]}',
                usage={},
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.SDKCodexSession",
        lambda *a, **k: _Session(),
    )
    try:
        created = runtime.start_operations_card(
            _scope(),
            card="payment",
            site_ids=(SITE,),
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        aid = created["diagnosis_id"]
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in {"completed", "failed", "inconclusive"}:
            assert time.monotonic() < deadline, current
            time.sleep(0.01)
            current = store.get_standard_diagnosis(aid, _scope().scope_fingerprint)
    finally:
        runtime.shutdown()

    assert current["status"] == "failed", current
    assert current["error_code"] == "ANALYSIS_FAILED", current


def test_a_half_narrative_is_not_a_narrative_either(tmp_path: Path, monkeypatch) -> None:
    """只带回**一个**散文字段的回包同样不算叙述（评审在 #718 上逮到的）。

    ``any(...)`` 的写法会让「只有 recommendation」过门，装配器把 ``main_cause`` 留成
    ``None``，作业照样 ``completed`` —— 与整份工具请求被当成叙述是同一类假陈述，
    只是少一个键而不是少两个。``OPS_NARRATIVE_SCHEMA`` 里两个字段都是 required，
    所以这条要求的正是声明过的东西。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)

    class _Session:
        def run(self, prompt: str, *, output_schema: Any = None) -> CodexTurnOutput:
            del prompt, output_schema
            return CodexTurnOutput(
                turn_id="t-1",
                final_response='{"recommendation": "先看站点 S-1"}',
                usage={},
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.SDKCodexSession",
        lambda *a, **k: _Session(),
    )
    try:
        created = runtime.start_operations_card(
            _scope(),
            card="payment",
            site_ids=(SITE,),
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        aid = created["diagnosis_id"]
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in {"completed", "failed", "inconclusive"}:
            assert time.monotonic() < deadline, current
            time.sleep(0.01)
            current = store.get_standard_diagnosis(aid, _scope().scope_fingerprint)
    finally:
        runtime.shutdown()

    assert current["status"] == "failed", current
    assert current["error_code"] == "ANALYSIS_FAILED", current


def test_the_whole_tenant_scope_is_no_narrowing_not_an_empty_one() -> None:
    """范围收敛的三值真值表（评审在 #721 上逮到的整条错路）。

    ``site_ids=None`` 在这条线上是**规范化谓词**，不是"没有范围"：它表示
    「租户内不限站点」，聚合层据此**不发**站点条件、只下推租户谓词。
    ``_intersect_sites`` 第一版把「两侧都是 ``None``」写成 ``()`` —— 而 ``()`` 在
    下游是「交集为空、什么都别读」，于是：

    * 失败率卡：**全租户账号恒得 ``SCOPE_EMPTY``**，`total=0`，一笔订单都不统计；
    * 三张卡（离线 / 故障码 / 支付）：走 ``_empty_card_result``，
      读者的「本窗无事发生」实际是「我们根本没查」。

    **这张表就是这个缺陷**：它只对一种账号出错（范围最宽的那个，也恰恰是功能为之
    存在的那个），而本文件此前每一处都在用 ``self`` 范围 + 显式站点集，
    两侧都非 ``None`` —— 那条路径一直是好的。加一条"整租户"的用例不是补充覆盖，
    是唯一能照到这个账号的位置。
    """
    from aiops_diagnostics.gateway_runtime import _intersect_sites

    assert _intersect_sites(None, None) is None, "整租户 = 不收窄，不是空集合"
    assert _intersect_sites(None, ("S-1", "S-2")) == ("S-1", "S-2")
    assert _intersect_sites(("S-1",), None) == ("S-1",)
    assert _intersect_sites(("S-1",), ("S-2",)) == (), "两侧都有值且不相交 = 真的空"
    assert _intersect_sites(("S-1", "S-2"), ("S-2", "S-1")) == ("S-1", "S-2")


def test_the_whole_tenant_caller_actually_gets_its_orders_counted(tmp_path: Path, monkeypatch) -> None:
    """整租户账号走完一整个作业：聚合**被调用**，且拿到的是「不收窄」。

    上一条钉住判据本身，这一条钉住它**接到哪儿**：真值表对了但接线处仍写着
    ``not effective_sites`` 的话，整租户账号照旧短路。两条一起才是一条能被证伪的修复。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "aiops_diagnostics.ops_analysis_narrative.run_narrative_turn",
        lambda *a, **k: {"main_cause": "成因", "recommendation": "建议"},
    )
    try:
        created = runtime.start_operations_analysis(
            _tenant_wide_scope(),
            site_ids=None,  # 管家端巡检：站点范围交给调用者自己的范围
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in {"completed", "failed", "inconclusive"}:
            assert time.monotonic() < deadline, current
            time.sleep(0.01)
            current = store.get_standard_diagnosis(
                created["diagnosis_id"], _tenant_wide_scope().scope_fingerprint
            )
    finally:
        runtime.shutdown()

    assert current["status"] == "completed", current
    result = current.get("result") or {}
    assert result.get("reason_code") is None, result
    assert result.get("counts", {}).get("total") == 10, result
    assert result.get("failure_rate") == 0.2, result
    # The decisive assertion: the aggregate ran, and it was asked for *no site
    # narrowing* — not for the empty set, which returns before any SQL.
    assert _SEEN_SITES == [None], _SEEN_SITES


def test_a_scope_resolution_failure_ends_the_job_instead_of_leaving_it_running(
    tmp_path: Path, monkeypatch
) -> None:
    """范围解析抛 ``ScopeError`` ⇒ 作业终结，**不是**停在 ``running``。

    ``ScopeError`` 继承 ``RuntimeError``，而两个 worker 的 ``except`` 收的是
    ``AgentRuntimeError, SourceError, ValueError, TypeError, sqlite3.OperationalError``
    —— 也就是说它**不在**那个元组里。而 ``resolve_query_scope`` 正是在 worker 的
    ``try`` 内被调用，它会抛 ``ScopeError``（数据范围超限、``self`` 范围但主体没绑
    C 端用户、代查缺 Dis 配置…），每一种都是普通的运行时状况，不是编程错误。

    这条判据是「**收敛到共用元组**」的直接反例：今天四个 worker 各自手写了一份
    异常元组（#700 的三次缺口正是这么来的），所以这里按**行为**钉住，
    而不是断言元组内容 —— 元组可以再漂。
    """
    runtime, store = _runtime(tmp_path, monkeypatch)
    from aiops_diagnostics.query_scope import MAX_SCOPE_IDS

    over_limit = tuple(f"S-{i}" for i in range(MAX_SCOPE_IDS + 1))
    try:
        created = runtime.start_operations_analysis(
            _scope(),
            site_ids=(SITE,),
            window_start=WINDOW[0],
            window_end=WINDOW[1],
            language="zh",
        )
        aid = created["diagnosis_id"]
        # Replace the resolved scope with one that overflows, so the NEXT resolve
        # inside the worker raises ScopeError. `_scope()` itself is well-formed;
        # this is the shape a real oversized data scope takes.
        monkeypatch.setattr(
            "aiops_diagnostics.gateway_runtime.resolve_query_scope",
            lambda *_a, **_k: (_ for _ in ()).throw(_scope_error(over_limit)),
        )
        deadline = time.monotonic() + 5
        current = created
        while current["status"] not in {"completed", "failed", "inconclusive"}:
            assert time.monotonic() < deadline, current
            time.sleep(0.01)
            current = store.get_standard_diagnosis(aid, _scope().scope_fingerprint)
    finally:
        runtime.shutdown()

    assert current["status"] == "failed", current
    assert current["error_code"] == "ANALYSIS_FAILED", current


def test_a_job_that_ran_out_its_deadline_is_visible_per_tenant(tmp_path: Path) -> None:
    """作业过期后，**这个租户**的指标里必须看得到它（#725 follow-up）。

    过期的作业发不出任何指标行：清扫是 `_expire_*` 里的裸 SQL，它不写
    `agent_run_metrics`。于是「跑完了 12 个、超时了 40 个」这种状态在指标面上读成
    「没有失败、没有取消」—— 而这正是 `mem-20260930-ranlei-008` 那条的形状
    （*只记失败侧的指标证明不了"在跑"*；这里反过来是*只记成功侧，看不见"没跑完"*）。

    这条判据同时钉住两件事：① 作业行**记下了租户**（在它之前只有不可逆的
    `scope_fingerprint`，所以过期根本归不到租户头上）；② 汇总把它算进来。
    """
    import json
    from datetime import UTC, datetime

    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.metrics_store import MetricsStore

    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(gateway_settings.database_file)
    runtime = GatewayRuntime(store, gateway_settings, Settings())

    job = store.create_analysis_job(
        "scope-fingerprint-1",
        site_ids=None,
        window_start="2026-10-01T00:00:00+00:00",
        window_end="2026-10-02T00:00:00+00:00",
        rule_version="ops-analysis-v1",
        language="zh",
        tenant_id=TENANT,
    )
    # The row really carries the tenant, not just the digest.
    row = store.get_standard_diagnosis(job["diagnosis_id"], "scope-fingerprint-1")
    assert row is not None
    with store._connection() as connection:  # noqa: SLF001 - the column is what is under test
        raw = connection.execute(
            "SELECT tenant_id FROM standard_diagnoses WHERE diagnosis_id = ?",
            (job["diagnosis_id"],),
        ).fetchone()
    assert raw["tenant_id"] == TENANT, dict(raw)

    # Force it past its deadline and sweep, exactly like the store's own path does.
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE standard_diagnoses SET status='running', deadline_at=?, expires_at=? "
            "WHERE diagnosis_id = ?",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", job["diagnosis_id"]),
        )
        store._expire_diagnoses(connection, datetime.now(UTC))  # noqa: SLF001
    swept = store.get_standard_diagnosis(job["diagnosis_id"], "scope-fingerprint-1")
    assert swept["status"] == "expired", swept

    # No metric row exists for it — that is the whole reason this needed a
    # different source.
    metrics = MetricsStore(gateway_settings.database_file)
    assert metrics.list_runs(TENANT, limit=10) == []

    viewer = ScopeContext.build(
        caller=SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT),
        subject=SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id=TENANT),
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="all"),
        roles=frozenset({"ROLE_AGENT_VIEWER"}),
        permissions=frozenset(),
    )
    summary = runtime.get_agent_metrics_summary(viewer)
    assert summary["totals"]["expired"] == 1, json.dumps(summary["totals"], ensure_ascii=False)

    # ① Another tenant's expired job must not appear in this tenant's payload, and
    #    the NULL-tenant rows (a single global number shared by every tenant) must
    #    not be reported AT ALL — it would take one tenant to leak every other
    #    tenant's job counts. Security finding in the #730/#731 reviews.
    other = store.create_analysis_job(
        "scope-fingerprint-2",
        site_ids=None,
        window_start="2026-10-01T00:00:00+00:00",
        window_end="2026-10-02T00:00:00+00:00",
        rule_version="ops-analysis-v1",
        language="zh",
        tenant_id="T-OTHER",
    )
    unattributed = store.create_analysis_job(
        "scope-fingerprint-3",
        site_ids=None,
        window_start="2026-10-01T00:00:00+00:00",
        window_end="2026-10-02T00:00:00+00:00",
        rule_version="ops-analysis-v1",
        language="zh",
        tenant_id=None,
    )
    with store._connection(write=True) as connection:  # noqa: SLF001
        for job_id in (other["diagnosis_id"], unattributed["diagnosis_id"]):
            connection.execute(
                "UPDATE standard_diagnoses SET status='running', deadline_at=?, expires_at=? "
                "WHERE diagnosis_id = ?",
                ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", job_id),
            )
        store._expire_diagnoses(connection, datetime.now(UTC))  # noqa: SLF001

    after = runtime.get_agent_metrics_summary(viewer)["totals"]
    assert after["expired"] == 1, after  # still only THIS tenant's
    assert "expired_unattributed" not in after, after

    # ② Under an agent filter every other number is narrowed to that agent, and
    #    job rows carry no agent id — so the count is omitted rather than
    #    reported as a number that means something else (the #731 BUG finding).
    filtered = runtime.get_agent_metrics_summary(viewer, agent_id="agt_deadbeef00000000")["totals"]
    assert "expired" not in filtered, filtered
