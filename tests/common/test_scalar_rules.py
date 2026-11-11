"""「一段外部数据读进来是什么意思」只有一处回答（#573）。

曾经时间解析七份、数值读取三份、存储写法五种、`gateway.db` 的连接
纪律三套——同一个 `created_time` 缺失，在六个实现里是「没有值」，在
`gateway_runtime._as_datetime` 里是把整单归成 ``SOURCE_UNAVAILABLE`` 且
可重试的 ``ValueError``。本文件钉住四件事：

* 读取语义——同一组输入在 ``as_datetime`` / ``as_integer`` 上给出唯一
  答案，且**永不抛异常**（「不抛」是合同，会抛的那一份曾经把缺失
  时间戳冒名成可重试的数据源故障）。
* 存储文本——``format_stored_time`` 写下的东西 ``parse_stored_time``
  读回同一时刻；两个 store 写同一列给出同一种文本。
* 连接行为——同库 store 共享行工厂、外键、等待上限；写块异常后不留
  半截写入；``SINGLE_STATEMENT`` profile 只有一个 store 选。
* 结构守卫——``fromisoformat`` / ``.isoformat()`` / ``sqlite3.connect``
  只允许出现在台账登记处；新增一份私有解析或私有连接让断言转红。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiops_diagnostics.common.scalar_rules import (
    as_datetime,
    as_integer,
    format_stored_time,
    now_utc,
    parse_stored_time,
)
from aiops_diagnostics.gateway import gateway_db

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"

# ---------------------------------------------------------------------------
# 一、读取语义：唯一答案 + 永不抛
# ---------------------------------------------------------------------------

_DATETIME_INPUTS = [
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00+05:00",
    "2026-01-01T00:00:00",  # naive → UTC
    " 2026-01-01T00:00:00+00:00 ",  # 首尾空白
    "",
    "   ",
    "not a date",
    "2026-13-99T99:99:99",
    None,
    42,
    b"2026-01-01T00:00:00",
    datetime(2026, 1, 1),  # naive datetime → UTC
    datetime(2026, 1, 1, tzinfo=UTC),
]


@pytest.mark.parametrize("value", _DATETIME_INPUTS)
def test_as_datetime_never_raises(value: object) -> None:
    result = as_datetime(value)
    assert result is None or result.tzinfo is not None  # 出口永远带时区


def test_as_datetime_pinned_answers() -> None:
    assert as_datetime("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert as_datetime("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert as_datetime(" 2026-01-01T00:00:00+05:00 ") == datetime(2025, 12, 31, 19, 0, 0, tzinfo=UTC)
    assert as_datetime("2026-01-01T05:00:00+05:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert as_datetime(None) is None
    assert as_datetime("") is None
    assert as_datetime("not a date") is None
    assert as_datetime(b"2026-01-01T00:00:00") is None
    # aware datetime 原样通过——不强行换区，比较与渲染各自负责
    aware = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
    assert as_datetime(aware) is aware


# 注意不能用 dict：`1 == 1.0 == True` 会让键互相覆盖。
_INTEGER_CASES = [
    (1, 1),
    (0, 0),
    ("1", 1),
    ("1.0", 1),
    ("3.0", 3),
    (" 5 ", 5),
    (1.0, 1),
    (-2, -2),
    # 其余一律 None
    ("3.5", None),
    ("abc", None),
    ("", None),
    ("  ", None),
    (None, None),
    (True, None),  # bool 不是整数
    (False, None),
    (3.5, None),
    ("inf", None),
    ("nan", None),
    ([1], None),
]


@pytest.mark.parametrize(("value", "expected"), _INTEGER_CASES)
def test_as_integer_one_answer(value: object, expected: int | None) -> None:
    assert as_integer(value) is expected


def test_as_integer_converged_semantics() -> None:
    """`int(float(x))` 与 `int(x)` 曾经分居两处，`"3.0"` 在两处答案相反。
    收敛后都走整值浮点串规则：可读成整数的读成整数，其余是不认识的值。"""
    assert as_integer("3.0") == 3  # 旧 int(value) 拒绝、int(float()) 接受 → 收敛为接受
    assert as_integer("3.5") is None  # 旧 int(float()) 截断成 3 → 收敛为不认识的值
    # order_status 的状态码判定共用这个读法：分数串不再被当成 code 3。
    from aiops_diagnostics.data.order_status import StatusRole, role_of

    assert role_of("5.0") is StatusRole.CONTROLLED_ABNORMAL_END
    assert role_of("3.5") is StatusRole.UNKNOWN


# ---------------------------------------------------------------------------
# 二、存储文本：一个时刻一种文本，写读同处定义
# ---------------------------------------------------------------------------


def test_stored_time_round_trips() -> None:
    for moment in (
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))),
        datetime(2026, 1, 1, 12, 0, 0),  # naive → UTC
    ):
        text = format_stored_time(moment)
        assert text.endswith("+00:00")
        expected = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
        assert parse_stored_time(text) == expected


def test_stored_time_one_text_regardless_of_zone() -> None:
    """同一时刻的不同偏移写法落成同一份文本——`+05:00` 与 `Z` 不再分两路。"""
    a = datetime(2026, 1, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    b = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert format_stored_time(a) == format_stored_time(b)


def test_parse_stored_time_is_the_strict_side() -> None:
    """存储读端读的是本模块写下的文本；读不出来是损坏，不是「没有值」。"""
    with pytest.raises(ValueError):
        parse_stored_time("not a timestamp")
    with pytest.raises(ValueError):
        parse_stored_time(None)
    # naive 旧文本按同一规则读回 UTC（既有库里的无偏移行不因此报错）
    assert parse_stored_time("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)


def test_two_stores_write_the_same_text(tmp_path: Path) -> None:
    """两个不同 store 往同一个库写时间列，文本是同一种形状。"""
    from aiops_diagnostics.conversation.conversation_store import ConversationStore
    from aiops_diagnostics.gateway.metrics_store import MetricsStore

    db = tmp_path / "gateway.db"
    conversations = ConversationStore(db)
    conversation = conversations.create(
        scope_fingerprint="f" * 64,
        business_entry="consumer",
        agent_version_key="agt_12345678#v1",
    )
    MetricsStore(db).record(
        tenant_id="t1",
        route_type="qa",
        outcome="completed",
    )
    with gateway_db.connection(db) as conn:
        created_text = conn.execute(
            "SELECT created_at FROM conversations WHERE conversation_id=?",
            (conversation["conversation_id"],),
        ).fetchone()[0]
        metric_text = conn.execute("SELECT created_at FROM agent_run_metrics LIMIT 1").fetchone()[0]
    assert created_text.endswith("+00:00") and metric_text.endswith("+00:00")
    # 两个文本都按权威规则读回，且读回的是同一刻度上的值。
    assert parse_stored_time(created_text) <= now_utc()
    assert parse_stored_time(metric_text) <= now_utc()


# ---------------------------------------------------------------------------
# 三、连接行为：同库共一套参数，profile 只能二选一
# ---------------------------------------------------------------------------

#: `gateway.db` 上的全部 store。新增的 store 只有两类结局：进本表选
#: profile，或进 `_CONNECT_LEDGER` 说明它开的是别的文件。
_STORE_SPECS = [
    ("GatewayStore", "aiops_diagnostics.gateway.gateway_store", "GatewayStore"),
    ("AgentStore", "aiops_diagnostics.publication.agent_lifecycle", "AgentStore"),
    ("ShortcutStore", "aiops_diagnostics.publication.shortcut_lifecycle", "ShortcutStore"),
    (
        "ConversationStore",
        "aiops_diagnostics.conversation.conversation_store",
        "ConversationStore",
    ),
    ("DifyRunStore", "aiops_diagnostics.dify.dify_run_store", "DifyRunStore"),
    ("AgentRunStore", "aiops_diagnostics.execution.agent_runs", "AgentRunStore"),
    ("DataGrantStore", "aiops_diagnostics.execution.chat_data_grant", "DataGrantStore"),
    ("DifyWorkspaces", "aiops_diagnostics.dify.dify_workspaces", "DifyWorkspaces"),
    ("DifyAppRegistry", "aiops_diagnostics.dify.dify_app_registry", "DifyAppRegistry"),
    ("ChatRouteBindingStore", "aiops_diagnostics.execution.chat_route_bindings", "ChatRouteBindingStore"),
    ("MetricsStore", "aiops_diagnostics.gateway.metrics_store", "MetricsStore"),
    ("SelfServiceStore", "aiops_diagnostics.dify.self_service_store", "SelfServiceStore"),
    ("KeyOperations", "aiops_diagnostics.dify.service_keys", "KeyOperations"),
    (
        "KnowledgeGrants",
        "aiops_diagnostics.consumer.consumer_knowledge_grants",
        "KnowledgeGrants",
    ),
    (
        "ConsumerEndpoints",
        "aiops_diagnostics.consumer.consumer_knowledge_grants",
        "ConsumerEndpoints",
    ),
]

#: metrics 的写恰好单语句——它选 SINGLE_STATEMENT，不是租约语义。
_SINGLE_STATEMENT_STORES = {"MetricsStore"}


def _cohort(db: Path) -> list[tuple[str, object]]:
    return [(name, getattr(importlib.import_module(module), cls)(db)) for name, module, cls in _STORE_SPECS]


def _reader(store: object):
    """该 store 的读连接。"""
    helper = getattr(store, "_connection", None) or store.connection
    return helper(write=False)


def _writer(store: object):
    """该 store 的写连接（LEASE；metrics 单独声明 SINGLE_STATEMENT）。"""
    helper = getattr(store, "_connection", None) or store.connection
    return helper(write=True) if "write" in inspect.signature(helper).parameters else helper()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "gateway.db"


def test_every_store_shares_the_connection_parameters(db_path: Path) -> None:
    for name, store in _cohort(db_path):
        with _reader(store) as conn:
            assert conn.row_factory is sqlite3.Row, name
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, name
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000, name


def test_lease_stores_roll_back_the_whole_write_block(db_path: Path) -> None:
    """LEASE：块里第二个语句炸了，第一个语句也不留——租约是整段的。"""
    for name, store in _cohort(db_path):
        if name in _SINGLE_STATEMENT_STORES:
            continue  # 单语句自带原子性，不是租约语义
        with pytest.raises(RuntimeError), _writer(store) as conn:
            conn.execute("CREATE TABLE rollback_probe (x)")
            raise RuntimeError("boom")
        with gateway_db.connection(db_path) as conn:
            assert (
                conn.execute("SELECT name FROM sqlite_master WHERE name='rollback_probe'").fetchone() is None
            ), name


def test_busy_wait_has_an_upper_bound(db_path: Path) -> None:
    """等待有上界：持锁方不放，写入方在 busy_timeout 内失败而不是死等。"""
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE IF NOT EXISTS lock_probe (x)")
    started = time.monotonic()
    try:
        with (
            pytest.raises(sqlite3.OperationalError),
            gateway_db.connection(db_path, write=True) as conn,
        ):
            conn.execute("INSERT INTO lock_probe VALUES (1)")
    finally:
        elapsed = time.monotonic() - started
        holder.rollback()
        holder.close()
    assert elapsed < 6.5  # busy_timeout=5000 + 余量；不是死等，也不是立刻放弃


def test_single_statement_is_a_declared_choice_not_an_omission(db_path: Path) -> None:
    """SINGLE_STATEMENT 下 metrics 照常落库，写后立即可见（自动提交），
    无需整段租约。"""
    from aiops_diagnostics.gateway.metrics_store import MetricsStore

    store = MetricsStore(db_path)
    store.record(tenant_id="t", route_type="qa", outcome="completed")
    with gateway_db.connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_run_metrics").fetchone()[0] == 1


def test_wal_is_set_by_every_initializer_not_by_construction_order(tmp_path: Path) -> None:
    """WAL 是文件属性：任意一个 store 先初始化，**它开的那个文件**都必须是
    WAL——不再依赖「GatewayStore 先建」这个顺序事实。每个 store 一个全新
    文件，防止前面的 store 替它答对了。"""
    for name, module, cls in _STORE_SPECS:
        store = getattr(importlib.import_module(module), cls)(tmp_path / f"{name}.db")
        with _reader(store) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal", name


# ---------------------------------------------------------------------------
# 四、结构守卫：私有解析 / 私有连接没有第三个住所
# ---------------------------------------------------------------------------

#: `fromisoformat` / `.isoformat()` 的合法居留点。每一行的身份：它回答的是
#: 别的问题，不是「存储时间文本」。新增一行要写出它校验/渲染的合同。
_IOFORMAT_LEDGER = {
    "common/scalar_rules.py",  # 权威本体
    "consumer/consumer_time.py",  # 展示层容错（读不出就显示原文）
    "consumer/consumer_agent.py",  # 工具中的CPMS原始日期；保留上游偏移，不能把naive业务时间冒充UTC
    "consumer/consumer_tool_grants.py",  # 节点请求的 expires_at 是 wire 合同
    "execution/chat_execution.py",  # 节点回调快照校验：合同要求**显式** UTC 偏移
    "health/health_report.py",  # 报告对外字段（started_at/stopped_at/data_as_of 是响应形状）
    "operator/ops_analysis.py",  # `_iso_z`：API 响应合同（Z 后缀由合同定义）
    "operator/operator_cpms.py",  # wire expires_at + observed_at 显式偏移校验（同 chat_execution）
    "operator/station_pricing.py",  # `date.fromisoformat`：用户入参的日期类型，不是存储时间
    "operator/station_tariff.py",  # 上游站点字段的日期类型
}

#: `sqlite3.connect` 的合法居留点。gateway.db 的连接只有 gateway_db 一份；
#: MappingStore 开独立 mappings.db，保留原始 sqlite 约定。
_CONNECT_LEDGER = {
    "gateway/gateway_db.py",
    "dify/dify_login_service.py",  # MappingStore → mappings.db
}

#: 「存储时间/数值读取」形态的函数名——曾经七种拼写各是一处语法。
#: `as_*`、`format_stored_time`、`parse_stored_time`、`now_utc` 是权威名字，
#: 不在禁表；`_ts` 是 parse_stored_time 的具名委托，同样合法。
_BANNED_SCALAR_DEFS = {
    "_iso",
    "_iso_z",
    "_parse_time",
    "_parse_iso",
    "_as_datetime",
    "_datetime",
    "_to_int",
    "_to_float",
    "_integer",
    "_decimal",
    "_as_bool",
    "_utc",
    "_now",
    "_utc_now",
}

#: `_iso_z` 是唯一的特许：API 响应合同（Z 后缀），语义台账见 _IOFORMAT_LEDGER。
_BANNED_DEF_EXEMPTIONS = {("operator/ops_analysis.py", "_iso_z")}


def _ioformat_sites(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"isoformat", "fromisoformat"}
        ):
            lines.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fromisoformat":
            lines.append(node.lineno)
    return lines


def test_no_private_time_or_scalar_grammar_exists() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = _ioformat_sites(tree)
        if lines and rel not in _IOFORMAT_LEDGER:
            offenders.extend(f"{rel}:{n}" for n in lines)
    assert offenders == [], (
        "fromisoformat/isoformat 只允许出现在台账登记处；存储时间文本一律走 "
        f"scalar_rules 的 format_stored_time/parse_stored_time。\n未登记：{offenders}"
    )


def test_no_private_scalar_parser_function_names() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _BANNED_SCALAR_DEFS
                and (rel, node.name) not in _BANNED_DEF_EXEMPTIONS
            ):
                offenders.append(f"{rel}:{node.lineno} {node.name}")
    assert offenders == [], f"私有标量解析名应进入 scalar_rules 或登记豁免：{offenders}"


def test_no_raw_sqlite_connect_on_the_shared_file() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        if rel in _CONNECT_LEDGER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"gateway.db 的连接一律走 gateway_db；别的文件进台账：{offenders}"


#: 台账：每个在同库文件上的 store 文件必须委托 gateway_db.connection。
_DELEGATED_STORE_FILES = {
    "gateway/gateway_store.py",
    "publication/agent_lifecycle.py",
    "publication/shortcut_lifecycle.py",
    "conversation/conversation_store.py",
    "dify/dify_run_store.py",
    "execution/agent_runs.py",
    "execution/chat_data_grant.py",
    "execution/chat_route_bindings.py",
    "dify/dify_workspaces.py",
    "dify/dify_app_registry.py",
    "gateway/metrics_store.py",
    "dify/self_service_store.py",
    "dify/service_keys.py",
    "consumer/consumer_knowledge_grants.py",
    "execution/node_capabilities.py",
}


def test_every_same_file_store_delegates_to_gateway_db() -> None:
    missing = []
    for rel in sorted(_DELEGATED_STORE_FILES):
        source = (SOURCE_ROOT / rel).read_text(encoding="utf-8")
        if "gateway_db.connection(" not in source:
            missing.append(rel)
    assert missing == [], f"同库 store 未委托 gateway_db.connection：{missing}"
    # SINGLE_STATEMENT 是具名选择：只允许在 metrics_store 与权威本体里出现。
    holders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SOURCE_ROOT))
        if "SINGLE_STATEMENT" in path.read_text(encoding="utf-8"):
            holders.append(rel)
    assert set(holders) == {"gateway/metrics_store.py", "gateway/gateway_db.py"}, holders


def test_gateway_ddl_has_one_authority() -> None:
    """持久结构只在 schema 模块声明；Mapping 是独立文件的具名例外。"""
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        rel = str(path.relative_to(SOURCE_ROOT))
        if rel in {"gateway/gateway_schema.py", "dify/dify_login_service.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "executescript":
                    offenders.append(f"{rel}:{node.lineno}")
                if (
                    node.func.attr == "execute"
                    and node.args
                    and any(
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and any(
                            ddl in value.value.upper()
                            for ddl in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX")
                        )
                        for value in ast.walk(node.args[0])
                    )
                ):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"Gateway DDL 必须委托唯一权威：{offenders}"


# ---------------------------------------------------------------------------
# 五、行为钉：二次读数拿到坏时间戳时，报告照常完成、遥测记 unavailable
# ---------------------------------------------------------------------------


def test_health_report_second_read_bad_timestamp_completes_with_telemetry_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build_minimal_health_report` 读到的订单通过了时间窗校验，而报告层
    随后**第二次**取数拿到的时间戳坏掉时：缺失时间戳不再被说成
    「数据源不可用、可重试」，报告以 telemetry=unavailable 落在
    completed。曾经 `_as_datetime` 抛 ValueError，落进外层通用异常归一类，
    作业 failed + SOURCE_UNAVAILABLE。"""
    from operator_support import context as make_context

    from aiops_diagnostics.common.config import SafetySettings
    from aiops_diagnostics.gateway import gateway_runtime
    from aiops_diagnostics.gateway.gateway_store import GatewayStore
    from aiops_diagnostics.identity.scope_context import DataScope

    ctx = make_context(DataScope(type="self"))
    store = GatewayStore(tmp_path / "gateway.db")
    job, _ = store.create_or_reuse_health_job(ctx.scope_fingerprint, "ORDER-1", "health-v2", tenant_id="t1")

    good_order = {
        "order_no": "ORDER-1",
        "status": 1,
        "device_code": "D-1",
        "created_time": "2026-09-01T01:00:00+00:00",
        "stop_time": "2026-09-01T02:00:00+00:00",
        "stopped_reason_code": "Local",
        "stopped_reason_content": "用户停止",
    }
    broken_order = {**good_order, "created_time": None}

    class _Sources:
        def __init__(self) -> None:
            self._reads = iter([[good_order], [broken_order]])

        def get_orders(self, order_no, tenant_id=None):
            return next(self._reads)

        def get_gun_samples(self, *args):
            raise AssertionError("缺时间戳时不应走到取数")

    @contextmanager
    def _fake_sources(*args, **kwargs):
        yield _Sources()

    monkeypatch.setattr(gateway_runtime, "scoped_live_sources", _fake_sources)
    monkeypatch.setattr(gateway_runtime, "resolve_query_scope", lambda *a, **k: object())

    runtime = gateway_runtime.GatewayRuntime.__new__(gateway_runtime.GatewayRuntime)
    runtime.store = store
    runtime.diagnostic_settings = type("S", (), {"safety": SafetySettings()})()
    runtime._execute_health_report(job["job_id"], ctx, "ORDER-1")

    row = store.get_health_job(job["job_id"], ctx.scope_fingerprint)
    assert row["status"] == "completed"
    assert row["report"]["source_summary"]["telemetry"] == "unavailable"
