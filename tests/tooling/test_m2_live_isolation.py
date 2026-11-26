"""隔离门禁的回归：任何一条不成立都必须在**打开库之前**拒绝。

判据现在只有一份（`aiops_diagnostics.gateway.isolation_guard`），真实启动路径与 preflight
共用它。这些用例从「其它前提全部成立」的基线逐项破坏，并断言对照文件原样不动。
临时目录扮演生产根（通过 `M2LIVE_FORBIDDEN` 追加），不需要真的读写 41。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from aiops_diagnostics.gateway.isolation_guard import (  # noqa: E402
    GuardSpec,
    IsolationError,
    assert_isolated,
    isolation_report,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "M2LIVE_ISOLATION_ROOT",
        "M2LIVE_READONLY_PATHS",
        "M2LIVE_EXTRA_WRITABLE",
        "M2LIVE_EXPECTED_UID",
        "M2LIVE_FORBIDDEN",
        "AIOPS_DATA_HOME",
        "AIOPS_GATEWAY_DATA_HOME",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_AGENT_RUN_ROOT",
        "AIOPS_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def layout(tmp_path):
    """baseline：一切合法的隔离根 + 一个「生产」目录（临时扮演）。"""
    root = tmp_path / "root"
    for name in ("data", "data/runs", "config/keys"):
        (root / name).mkdir(parents=True, exist_ok=True)
    prod = tmp_path / "prod"
    (prod / "gateway").mkdir(parents=True)
    (prod / "production.env").write_text("AIOPS_X=1\n")
    readonly = root / "readonly-production.env"
    readonly.write_text("AIOPS_X=1\n")
    return root, prod, readonly


def _spec(root: Path, prod: Path, **overrides) -> GuardSpec:
    writable = {
        "data_home": root / "data",
        "database_file": root / "data/gateway.db",
        "run_root": root / "data/runs",
        "key_home": root / "config/keys",
    }
    writable.update(overrides.pop("writable", {}))
    return GuardSpec(
        root=root,
        writable=writable,
        forbidden=(prod,),
        **overrides,
    )


def test_the_baseline_is_accepted(layout):
    root, prod, _ = layout
    resolved = assert_isolated(_spec(root, prod))
    assert resolved["database_file"].is_relative_to(root.resolve())


def test_a_database_outside_the_root_is_refused(layout):
    root, prod, _ = layout
    with pytest.raises(IsolationError):
        assert_isolated(_spec(root, prod, writable={"database_file": prod / "gateway/gateway.db"}))


def test_an_extra_writable_mount_outside_the_root_is_refused(layout):
    """只核对 Gateway 自己的四个路径会漏掉平台库/平台状态/节点 home。"""
    root, prod, _ = layout
    with pytest.raises(IsolationError):
        assert_isolated(_spec(root, prod, writable={"mount_0": prod / "elsewhere"}))


def test_a_symlink_into_production_is_refused(layout):
    root, prod, _ = layout
    link = root / "linked"
    link.symlink_to(prod / "gateway")
    with pytest.raises(IsolationError):
        assert_isolated(_spec(root, prod, writable={"data_home": link}))


def test_a_root_that_is_itself_a_symlink_is_refused(layout, tmp_path):
    """声明的隔离根本身是链接时必须拒绝 —— 判定要在 `resolve()` **之前**做。

    先 `resolve()` 再问 `is_symlink()` 永远是 False（解析结果不是链接），这条测试在
    那个顺序下会失败：链接指向哪里都不重要，声明路径本身就不是一个真正的目录。
    """
    real = tmp_path / "real-root"
    for name in ("data", "data/runs", "config/keys"):
        (real / name).mkdir(parents=True, exist_ok=True)
    link = tmp_path / "linked-root"
    link.symlink_to(real)
    with pytest.raises(IsolationError, match="符号链接"):
        assert_isolated(_spec(link, tmp_path / "prod"))


def test_a_root_inside_the_forbidden_prefix_is_refused(layout):
    _, prod, _ = layout
    inner = prod / "inner"
    inner.mkdir()
    with pytest.raises(IsolationError):
        assert_isolated(_spec(inner, prod))


def test_the_parent_of_production_cannot_be_a_root(layout):
    root, prod, _ = layout
    with pytest.raises(IsolationError):
        assert_isolated(_spec(root.parent, prod))


def test_missing_required_paths_are_refused(layout):
    root, prod, _ = layout
    with pytest.raises(IsolationError, match="key_home"):
        assert_isolated(
            GuardSpec(
                root=root,
                writable={
                    "data_home": root / "data",
                    "database_file": root / "data/gateway.db",
                    "run_root": root / "data/runs",
                },
                forbidden=(prod,),
            )
        )


def test_a_readonly_dependency_must_exist_and_be_readable(layout):
    root, prod, readonly = layout
    with pytest.raises(IsolationError, match="只读依赖不存在"):
        assert_isolated(_spec(root, prod, readonly=(root / "missing.env",)))
    os.chmod(readonly, 0o000)
    try:
        with pytest.raises(IsolationError, match="不可读"):
            assert_isolated(_spec(root, prod, readonly=(readonly,)))
    finally:
        os.chmod(readonly, 0o600)


def test_a_readonly_dependency_may_live_outside_the_root(layout):
    """生产配置是只读依赖，不是可写状态：它在根之外是正常的。"""
    root, prod, _ = layout
    resolved = assert_isolated(_spec(root, prod, readonly=(prod / "production.env",)))
    assert resolved["database_file"].is_relative_to(root.resolve())


def test_readonly_and_writable_must_not_overlap(layout):
    root, prod, _ = layout
    with pytest.raises(IsolationError, match="重叠"):
        assert_isolated(_spec(root, prod, readonly=(root / "data",)))


def test_the_expected_uid_is_enforced(layout):
    root, prod, _ = layout
    other = os.getuid() + 1
    with pytest.raises(IsolationError, match="运行 UID"):
        assert_isolated(_spec(root, prod, expected_uid=other))
    assert assert_isolated(_spec(root, prod, expected_uid=os.getuid()))


def test_the_report_never_contains_absolute_paths(layout):
    root, prod, _ = layout
    report = isolation_report(root, assert_isolated(_spec(root, prod)))
    assert str(root) not in json.dumps(report)
    assert report["root"] == "."


def test_the_guard_fails_before_any_database_is_opened(layout, tmp_path):
    """误配置不能有副作用：对照库里的在途记录与 mtime 都不变。"""
    root, prod, _ = layout
    control = prod / "gateway" / "gateway.db"
    connection = sqlite3.connect(control)
    try:
        connection.execute("CREATE TABLE health_report_jobs (job_id TEXT, status TEXT)")
        connection.executemany(
            "INSERT INTO health_report_jobs VALUES (?, ?)",
            [("job-1", "queued"), ("job-2", "running")],
        )
        connection.commit()
    finally:
        connection.close()
    before_mtime = control.stat().st_mtime_ns
    before_rows = sqlite3.connect(control).execute("SELECT * FROM health_report_jobs").fetchall()

    with pytest.raises(IsolationError):
        assert_isolated(_spec(root, prod, writable={"database_file": control}))

    assert control.stat().st_mtime_ns == before_mtime
    assert sqlite3.connect(control).execute("SELECT * FROM health_report_jobs").fetchall() == before_rows


def test_gateway_serve_calls_the_guard_before_creating_the_app():
    """真实启动路径必须真的调用它 —— 只写在 README 里不算。"""
    source = (REPO / "src/aiops_diagnostics/gateway_server.py").read_text(encoding="utf-8")
    serve = source.split("def serve(", 1)[1].split("@app.command", 1)[0]
    assert "enforce_from_env" in serve, "serve 没有调用隔离门禁"
    assert serve.index("enforce_from_env") < serve.index("GatewayServerSettings.from_env()"), (
        "门禁必须在读取/构造设置之前"
    )
    assert serve.index("enforce_from_env") < serve.index("create_gateway_app"), (
        "门禁必须在创建应用（会打开库）之前"
    )


def test_the_cli_reports_refusal_on_stdout_and_exits_nonzero(layout):
    root, prod, readonly = layout
    env = {
        **os.environ,
        "M2LIVE_ISOLATION_ROOT": str(root),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(prod / "gateway/gateway.db"),
        "AIOPS_AGENT_RUN_ROOT": str(root / "data/runs"),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "M2LIVE_FORBIDDEN": str(prod),
    }
    result = subprocess.run(
        [sys.executable, "-m", "aiops_diagnostics.gateway.isolation_guard"],
        env={**env, "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "REFUSED"


def test_without_the_root_variable_the_guard_is_a_no_op():
    """生产 Gateway 不设这个变量：启动路径必须逐字不变。"""
    from aiops_diagnostics.gateway.isolation_guard import enforce_from_env

    assert enforce_from_env() is None
