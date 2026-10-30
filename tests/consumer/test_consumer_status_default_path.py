"""默认适配路径的测试：走**真实的 `default_deps`**，配本地合成数据库。

上一版的缺陷：`default_deps.list_orders` 原样转交 `MySQLSource.list_orders()` 的行，
而那些行**没有 `id`**（公开契约只输出 `order_no/status/site_id/created_time`）。
排序判据要 `(created_time, id)`，于是**正常的多行结果必然** `missing_id`、退 2。

这里的做法：不注入 Deps 替身、不提前补 `id`。取而代之的是把 MySQL 驱动换成
一张**本地合成表**，让 `MySQLSource` 真的跑它的 SQL、真的按它渲染出的 WHERE 选行，
再让 `default_deps` 自己把缺的 `id` 补上。这样验证的是**适配器本身**，
包括"补不上就让它缺着"的那条路。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools" / "verification" / "consumer-status"

TENANT = "T-1"
C_USER = "c-1"
WORK_ORDER_SENTINEL = -1


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"cs3_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cpms = _load("independent_cpms")


class FakeCursor:
    """按渲染出的 WHERE 从本地合成表选行 —— 不解析 SQL 文本做断言，只做行级镜像。"""

    def __init__(self, table: list[dict[str, Any]]) -> None:
        self.table = table
        self.executed: list[tuple[str, list[Any]]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))

    def fetchall(self) -> list[dict[str, Any]]:
        # 游标先执行过 SET SESSION / START TRANSACTION，这里要挑出**真正的查询**。
        query = next(
            ((sql, params) for sql, params in reversed(self.executed) if "ch_order_info" in sql),
            None,
        )
        if query is None:
            return []
        sql, params = query
        if " WHERE " not in sql:
            rows = list(self.table)
        else:
            where = sql.split(" WHERE ", 1)[1].rsplit(" ORDER BY ", 1)[0]
            if "1=0" in where:
                return []
            rows = list(self.table)
            remaining = list(params)
            for fragment in where.split(" AND "):
                if fragment.startswith("site_id IN ("):
                    sites = {str(remaining.pop(0)) for _ in range(fragment.count("%s"))}
                    rows = [r for r in rows if str(r.get("site_id")) in sites]
                elif fragment.startswith("(CASE WHEN user_id=%s THEN consumer_id"):
                    sentinel = str(remaining.pop(0))
                    wanted = str(remaining.pop(0))
                    rows = [
                        r
                        for r in rows
                        if (
                            str(r.get("consumer_id"))
                            if str(r.get("user_id")) == sentinel
                            else str(r.get("user_id"))
                        )
                        == wanted
                    ]
                else:
                    value = str(remaining.pop(0))
                    if fragment == "order_no=%s":
                        rows = [r for r in rows if str(r.get("order_no")) == value]
                    elif fragment == "tenant_id=%s":
                        rows = [r for r in rows if str(r.get("tenant_id")) == value]
                    elif fragment == "user_id=%s":
                        rows = [r for r in rows if str(r.get("user_id")) == value]
                    else:  # pragma: no cover - 片段形状变了就该失败
                        raise AssertionError(f"unexpected scope fragment: {fragment}")
        # 与生产同序：created_time DESC, id DESC。
        # 没有 id 的行按 0 参与排序（模拟"库里这一列没取到"），不在这里补。
        rows.sort(
            key=lambda r: (str(r.get("created_time", "")), int(r.get("id") or 0)),
            reverse=True,
        )
        # **按 SELECT 列表裁剪**：真实数据库不会把没选的列塞回来。
        # `list_orders` 只选 order_no/status/site_id/created_time —— 所以它**确实**
        # 拿不到 id。替身若把整行原样返回，就掩盖了这条真实形状，变异也就测不出来。
        selected = _selected_columns(sql)
        return [{k: r[k] for k in selected if k in r} for r in rows]

    def fetchone(self) -> None:
        return None


_COLUMN_NAMES = (
    "id",
    "order_no",
    "tenant_id",
    "status",
    "type",
    "billing_type",
    "launch_type",
    "is_test",
    "device_id",
    "device_code",
    "child_device_id",
    "child_device_code",
    "site_id",
    "created_time",
    "user_id",
    "consumer_id",
)


def _selected_columns(sql: str) -> set[str]:
    """从 SELECT 列表里认出真正被选中的列（只认已知列名，不假装是 SQL 解析器）。

    **按完整标识符匹配**：`"id" in head` 会命中 `site_id` / `consumer_id` 里的子串，
    于是"只选了四列"的查询被判成"选了 id"，替身就又比真库宽容了 —— 这个坑当场
    把变异测试骗绿过一次。
    """
    head = sql.split(" FROM ", 1)[0]
    tokens = {token.strip("`,() ") for token in head.replace("\n", " ").replace(",", " ").split()}
    return {name for name in _COLUMN_NAMES if name in tokens}


class FakeConnection:
    def __init__(self, table: list[dict[str, Any]]) -> None:
        self.cursor_obj = FakeCursor(table)

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _rows_with_id() -> list[dict[str, Any]]:
    return [
        {
            "id": 42,
            "order_no": "SYNTH-A",
            "tenant_id": TENANT,
            "status": 1,
            "site_id": "S-1",
            "user_id": C_USER,
            "created_time": "2026-10-23 10:00:00",
        },
        {
            "id": 41,
            "order_no": "SYNTH-B",
            "tenant_id": TENANT,
            "status": 1,
            "site_id": "S-1",
            "user_id": C_USER,
            "created_time": "2026-10-23 10:00:00",  # 同秒，靠 id 分先后
        },
    ]


class _Settings:
    """`default_deps` 需要的两项：mysql 凭据与 safety 阈值。"""

    class mysql:  # noqa: N801
        host = "127.0.0.1"
        port = 3306
        user = "readonly"
        password = "secret"
        database = "qumall_mall"

    class safety:  # noqa: N801
        query_timeout_seconds = 5
        mysql_max_execution_ms = 5000

    class redis:  # noqa: N801
        host = "127.0.0.1"
        port = 6379
        database = 0
        user = None
        password = ""


def _install_default_deps(monkeypatch: pytest.MonkeyPatch, table: list[dict[str, Any]]):
    """把 `default_deps` 的外部世界换成合成库，**不动它的适配逻辑**。

    只替换三样：MySQL 驱动（换成合成表）、身份解析、端点与 CPMS。
    `default_deps` 里"用 `get_orders` 补 `id`"的那段代码**原样执行** —— 那正是被测对象。
    """
    from aiops_diagnostics.data import sources as sources_module

    conn = FakeConnection(table)
    monkeypatch.setattr(sources_module.pymysql, "connect", lambda **_: conn, raising=True)
    monkeypatch.setattr(cpms, "_resolve_caller", lambda *_a, **_k: _caller())

    real_default = cpms.default_deps

    def deps_factory(*, config_file: str, session: str):
        # 先拿到真实的 deps（它会 import Settings/GatewayServerSettings）；
        # 用最小替身顶掉那两处读取，其余（补 id 那段）不动。
        monkeypatch.setattr(
            "aiops_diagnostics.common.config.Settings.from_config",
            classmethod(lambda _cls, _path: _Settings()),
        )
        monkeypatch.setattr(
            "aiops_diagnostics.gateway.gateway_config.GatewayServerSettings.from_env",
            classmethod(lambda _cls: _GatewaySettings()),
        )

        def scoped(_settings, _scope):
            return _Ctx(sources_module, _Settings, _QueryScope())

        monkeypatch.setattr(sources_module, "scoped_mysql_source", scoped)

        from aiops_diagnostics.data import charging_status

        monkeypatch.setattr(
            charging_status,
            "read_charging_status",
            lambda endpoint, order_no, *, tenant_id, user_id: _cpms_result(order_no),
        )
        return real_default(config_file=config_file, session=session)

    monkeypatch.setattr(cpms, "default_deps", deps_factory)
    return conn


def _cpms_result(order_no: str) -> dict[str, Any]:
    return {
        "order_no": order_no,
        "status_code": "1",
        "status_role": "finished",
        "observed_at": "2026-10-23T00:00:00+00:00",
        "source": "cpms.chargingInfo",
    }


class _Ctx:
    def __init__(self, sources_module, settings_cls, scope) -> None:
        self._sources = sources_module
        self._settings = settings_cls
        self._scope = scope

    def __enter__(self):
        return self._sources.MySQLSource(self._settings, scope=self._scope)

    def __exit__(self, *_args: Any) -> bool:
        return False


class _QueryScope:
    tenant_id = TENANT
    site_ids = None
    user_id = C_USER
    empty_site_scope = False


class _GatewaySettings:
    charging_status_base_url = "T-1:https://cpms.example/charging-pile"


def _caller():
    """用**真实的 `ScopeContext`**，不手造属性形状。"""
    from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id=None, c_user_id=C_USER, tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self", site_ids=()),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
    )


@pytest.fixture(autouse=True)
def _session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_CHECK_SESSION", "sess")


def test_default_adapter_supplies_real_id_and_ordering_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**默认适配路径**：真实 `default_deps` + 合成库 ⇒ 排序能过、退出 0。

    这就是上一版坏掉的那条路：`list_orders` 不带 id，`default_deps` 原样转交，
    `sort_verdict` 判 `missing_id` ⇒ 正常结果也退 2。
    """
    _install_default_deps(monkeypatch, _rows_with_id())
    code = cpms.main([])
    out = capsys.readouterr().out
    assert code == cpms.EXIT_PASS, out
    assert '"verdict": "pass"' in out


def test_default_adapter_leaves_id_missing_when_it_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """补不上 id 时**不伪造**：结果是 unverified，不是 pass。"""
    table = _rows_with_id()
    for row in table:
        row.pop("id")
    _install_default_deps(monkeypatch, table)
    code = cpms.main([])
    out = capsys.readouterr().out
    assert code == cpms.EXIT_UNVERIFIED, out
    assert "missing_id" in out


def test_default_adapter_respects_the_database_ordering_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """倒序（id 小的在前）必须被判失败 —— 判据跟着库的实际排序走。"""
    table = _rows_with_id()
    # 让合成库把「旧的」排在前面：直接改 created_time，让顺序真的反了
    table[0]["created_time"] = "2026-10-22 10:00:00"
    table[1]["created_time"] = "2026-10-23 10:00:00"
    conn = _install_default_deps(monkeypatch, table)
    # 合成库自己会按 ORDER BY 排好，所以这里改成「库里就是错的顺序」：
    # 直接把 MySQLSource 返回的形状喂成非降序 —— 用假的 fetchall 覆盖。
    original = conn.cursor_obj.fetchall

    def reversed_fetchall():
        rows = original()
        return list(reversed(rows))

    monkeypatch.setattr(conn.cursor_obj, "fetchall", reversed_fetchall)
    code = cpms.main([])
    out = capsys.readouterr().out
    assert code == cpms.EXIT_FAIL, out
    assert "not_strictly_descending" in out


# ---------------------------------------------------------------- 浏览器驱动回放


def test_offline_drive_replay_passes_all_three_scenarios() -> None:
    """用**实际 `_drive()`** 跑离线回放：正常/逐步渲染/错误 QA 三种。

    跳过条件是"这台机器没装 playwright 浏览器" —— 那确实无法运行，
    但**不是**"需要开生产窗口"。回放本身不碰生产：所有 `/v1/` 在路由层拦截，
    未声明的直接 abort，`production_business_requests` 恒为 0。
    """
    import subprocess

    script = TOOLS / "replay_drive.py"
    assert script.exists()
    # 找一个**装了 playwright 的**解释器：它常装在系统/conda python 里，
    # 而项目 venv 未必装。逐个探测，不假定 `sys.executable` 就是对的。
    candidates = ["/usr/bin/python3", "/home/claude/miniconda3/bin/python3", sys.executable]
    interpreter = None
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            interpreter = candidate
            break
    if interpreter is None:
        pytest.skip("找不到装了 playwright 的解释器")
    result = subprocess.run([interpreter, str(script)], capture_output=True, text=True, timeout=300)
    if "Executable doesn't exist" in result.stderr:
        pytest.skip("playwright 浏览器未下载")
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "pass", result.stdout
    assert payload["production_business_requests"] == 0
    scenarios = payload["scenarios"]
    assert set(scenarios) == {"normal", "progressive", "failed", "node"}
    # 正常与逐步渲染：判据全过
    assert scenarios["normal"]["all_checks"] is True
    assert scenarios["progressive"]["all_checks"] is True
    assert scenarios["node"]["all_checks"] is True
    # 错误 QA：判据必须至少有一项不过
    assert scenarios["failed"]["all_checks"] is False
    assert "get_completed" in scenarios["failed"]["failing"]
