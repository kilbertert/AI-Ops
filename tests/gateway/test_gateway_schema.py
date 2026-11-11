"""#1448 判据：确认规格 + 固定历史 DDL，不能由候选删列生成历史。"""

import importlib
import json
import multiprocessing
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

import pytest
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway import gateway_schema as schema
from gateway.test_database_initialization import LEGACY_DIFY, _snapshot

# 44260454:src/aiops_diagnostics/agent_runs.py 的原始建表合同。
LEGACY_AGENT = """
CREATE TABLE agent_runs (
    run_id TEXT PRIMARY KEY, scope_fingerprint TEXT NOT NULL, scope_json TEXT NOT NULL,
    order_no TEXT NOT NULL, operation TEXT NOT NULL, environment TEXT NOT NULL,
    app_id TEXT NOT NULL, agent_id TEXT NOT NULL, expected_node_id TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, snapshot_json TEXT, actual_node_id TEXT, node_build TEXT,
    gateway_version TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    claimed_at TEXT, completed_at TEXT
)
"""


def historical(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(LEGACY_DIFY)
        db.execute(LEGACY_AGENT)
        db.execute("CREATE TABLE historic_notes (note TEXT)")
        db.execute("INSERT INTO historic_notes VALUES ('preserve')")
        db.execute(
            "INSERT INTO agent_runs(run_id,scope_fingerprint,scope_json,order_no,operation,"
            "environment,app_id,agent_id,expected_node_id,state,gateway_version,created_at,expires_at) "
            "VALUES('r','fingerprint','{}','order','charging_status','env','app','agent','node',"
            "'issued','1.0','2026-01-01','2026-01-02')"
        )


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return _snapshot(path), version


def hold_writer(path, ready, release):
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        ready.set()
        release.wait(10)
        db.rollback()


def migrate_worker(path, barrier, queue):
    original = gateway_db.initialize
    versions = []

    @contextmanager
    def synchronized_initialize(path):
        # Both processes finish the version fast-path before either obtains the lease.
        barrier.wait(5)
        with original(path) as conn:
            conn.set_trace_callback(
                lambda sql: versions.append(sql) if sql.startswith("PRAGMA user_version=") else None
            )
            yield conn

    gateway_db.initialize = synchronized_initialize
    try:
        schema.startup(path)
        queue.put(("ok", len(versions)))
    except Exception as exc:
        queue.put((repr(exc), len(versions)))


def test_current_constructors_read_under_another_process_writer(tmp_path, monkeypatch):
    from common.test_scalar_rules import _STORE_SPECS, _reader
    from execution.test_agent_run_records import _config, _scope

    from aiops_diagnostics.dify.dify_run_store import DifyRunStore
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.execution.agent_runs import AgentRunConfig, AgentRunStore

    path = tmp_path / "gateway.db"
    schema.ensure(path)
    agent = AgentRunStore(path).issue(
        scope=_scope(),
        order_no="ORDER-A",
        config=AgentRunConfig.load(_config(tmp_path)),
        gateway_version="test",
    )
    DifyRunStore(path).issue(
        run_id="dify-run",
        qa_id="qa",
        scope_fingerprint="scope",
        scope_json="{}",
        binding_key="binding",
        order_no="ORDER-A",
        environment="env-a",
        tenant_id="tenant",
        app_id="app",
        workflow_id="flow",
        grant_id="grant",
    )
    SelfServiceStore(path).register(
        app_id="app",
        tenant_id="tenant",
        workspace_id="workspace",
        agent_name="agent",
        profile_id="profile",
        actor="test",
    )
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold_writer, args=(path, ready, release))
    process.start()
    traces = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(traces.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        assert ready.wait(5)
        start = time.monotonic()
        for _, module, cls in _STORE_SPECS:
            store = getattr(importlib.import_module(module), cls)(path)
            with _reader(store) as conn:
                assert conn.execute("SELECT 7").fetchone()[0] == 7
        assert AgentRunStore(path).peek(agent.run_id).order_no == "ORDER-A"
        assert DifyRunStore(path).peek("dify-run").order_no == "ORDER-A"
        assert SelfServiceStore(path).app("app")["tenant_id"] == "tenant"
        assert time.monotonic() - start < 1
        assert not any(
            sql.lstrip().upper().startswith(("BEGIN", "CREATE", "ALTER", "UPDATE")) for sql in traces
        )
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0


@pytest.mark.parametrize("historical_db", [False, True])
def test_two_processes_initialize_once(tmp_path, historical_db):
    path = tmp_path / "gateway.db"
    if historical_db:
        historical(path)
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=migrate_worker, args=(path, barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        assert sorted(queue.get(timeout=12) for _ in processes) == [("ok", 0), ("ok", 1)]
    finally:
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
    assert schema.check(path)["version"] == schema.VERSION


@pytest.mark.parametrize("fail_after", ["CREATE TABLE", "ADD COLUMN", "CREATE INDEX", "PRAGMA user_version="])
def test_failure_after_each_migration_stage_is_atomic(tmp_path, monkeypatch, fail_after):
    path = tmp_path / "gateway.db"
    historical(path)
    before = snapshot(path)
    original = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            # Only the real migration connection, not the in-memory reference schema.
            if self.in_transaction and fail_after in sql:
                raise RuntimeError("injected after write")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Failing, **kw))
        with pytest.raises(RuntimeError, match="injected"):
            schema.ensure(path)
    assert snapshot(path) == before
    schema.ensure(path)
    assert schema.check(path)["version"] == schema.VERSION


def test_pinned_history_preserves_data_and_default_contract(tmp_path):
    old, fresh = tmp_path / "old.db", tmp_path / "fresh.db"
    historical(old)
    schema.ensure(old)
    schema.ensure(fresh)
    with gateway_db.connection(old) as db:
        assert tuple(
            db.execute(
                "SELECT run_id,scope_json,expected_agent_version,trace_json FROM agent_runs"
            ).fetchone()
        ) == ("r", "{}", None, None)
        assert db.execute("SELECT note FROM historic_notes").fetchone()[0] == "preserve"
    assert schema.check(old)["warnings"] == ["extra table: historic_notes"]
    with gateway_db.connection(old) as a, gateway_db.connection(fresh) as b:
        for table in schema.TABLES:
            assert schema._columns(a, table) == schema._columns(b, table)
            assert schema._indexes(a, table) == schema._indexes(b, table)


@pytest.mark.parametrize(
    "mutation",
    [
        "PRAGMA user_version=999",
        "PRAGMA user_version=-1",
        "ALTER TABLE agent_runs DROP COLUMN trace_json",
        "CREATE INDEX unknown_index ON agent_runs(state)",
        "DROP INDEX idx_agent_runs_scope; "
        "CREATE INDEX idx_agent_runs_scope ON agent_runs(created_at,scope_fingerprint)",
        "DROP INDEX idx_agent_runs_scope; "
        "CREATE INDEX idx_agent_runs_scope ON agent_runs(scope_fingerprint,created_at)",
        "DROP INDEX idx_agent_runs_scope; "
        "CREATE INDEX idx_agent_runs_scope ON agent_runs(scope_fingerprint COLLATE NOCASE,created_at DESC)",
        "DROP INDEX idx_agent_runs_scope; "
        "CREATE INDEX idx_agent_runs_scope ON agent_runs(scope_fingerprint,created_at DESC) "
        "WHERE state='issued'",
        "CREATE TRIGGER unexpected AFTER INSERT ON agent_runs BEGIN DELETE FROM agent_runs; END",
    ],
)
def test_startup_rejects_current_shape_drift_without_changing_it(tmp_path, mutation):
    path = tmp_path / "gateway.db"
    schema.ensure(path)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(mutation)
    before = snapshot(path)
    with pytest.raises(schema.SchemaError):
        schema.startup(path)
    assert snapshot(path) == before


@pytest.mark.parametrize("suffix", ["CHECK(stage != 'no')", "UNIQUE", "COLLATE NOCASE"])
def test_unknown_constraints_are_not_inferred_from_pragma(tmp_path, suffix):
    path = tmp_path / "gateway.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE service_key_ops(app_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,"
            f"before_json TEXT NOT NULL,stage TEXT NOT NULL {suffix},updated_at REAL NOT NULL)"
        )
    before = snapshot(path)
    with pytest.raises(schema.SchemaError):
        schema.ensure(path)
    assert snapshot(path) == before


def test_cli_check_is_readonly_and_wrong_database_is_refused(tmp_path):
    path = tmp_path / "absent.db"
    runner = CliRunner()
    assert runner.invoke(admin_app, ["schema", "check", "--db", str(path)]).exit_code == 1
    assert not path.exists()
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE other_app (secret TEXT)")
    before = snapshot(path)
    result = runner.invoke(admin_app, ["schema", "migrate", "--db", str(path)])
    assert result.exit_code == 1
    assert snapshot(path) == before
    path.unlink()
    assert runner.invoke(admin_app, ["schema", "migrate", "--db", str(path)]).exit_code == 0
    before = snapshot(path)
    result = runner.invoke(admin_app, ["schema", "check", "--db", str(path)])
    assert json.loads(result.stdout)["version"] == schema.VERSION
    assert snapshot(path) == before


def test_rehearsal_preserves_wal_rows_and_does_not_migrate_source(tmp_path):
    source, backup = tmp_path / "gateway.db", tmp_path / "backup.db"
    historical(source)
    with closing(sqlite3.connect(source, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO historic_notes VALUES('wal-only')")
        before = snapshot(source)
        schema.rehearse(source, backup)
        assert snapshot(source) == before
        assert snapshot(backup) == before
    with pytest.raises(FileExistsError):
        schema.rehearse(source, backup)


def test_repeated_concurrent_wal_initialization(tmp_path):
    # 父反例：SQLite3.45.1 的40轮双线程18轮BUSY；不能用一次碰巧成功替代。
    errors = []
    for iteration in range(40):
        path = tmp_path / str(iteration) / "gateway.db"
        barrier = threading.Barrier(2)

        def run(path=path, barrier=barrier):
            try:
                barrier.wait(5)
                schema.ensure(path)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("code,retried", [(sqlite3.SQLITE_BUSY, True), (sqlite3.SQLITE_LOCKED, False)])
def test_wal_only_retries_busy(tmp_path, monkeypatch, code, retried):
    original = sqlite3.connect
    attempts = []

    class BusyOnce(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                if len(attempts) == 1:
                    exc = sqlite3.OperationalError("synthetic")
                    exc.sqlite_errorcode = code
                    raise exc
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=BusyOnce, **kw))
    if retried:
        schema.ensure(tmp_path / "gateway.db")
        assert len(attempts) == 2
    else:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            schema.ensure(tmp_path / "gateway.db")
        assert len(attempts) == 1


def test_scope_default_exception_is_exact_and_reordered_columns_are_accepted(tmp_path):
    for default in (" DEFAULT ''", " DEFAULT '{}'", ""):
        path = tmp_path / (str(len(default)) + ".db")
        # Independent pinned DDL uses only a documented historical default variation.
        ddl = LEGACY_DIFY.replace("scope_json TEXT NOT NULL", "scope_json TEXT NOT NULL" + default)
        with closing(sqlite3.connect(path)) as db:
            db.execute(ddl)
        if default == " DEFAULT '{}'":
            with pytest.raises(schema.SchemaError):
                schema.ensure(path)
        else:
            schema.ensure(path)
            assert bool(schema.check(path)["warnings"]) == bool(default)
    path = tmp_path / "reordered.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE service_key_ops(updated_at REAL NOT NULL,stage TEXT NOT NULL,"
            "before_json TEXT NOT NULL,workspace_id TEXT NOT NULL,app_id TEXT PRIMARY KEY)"
        )
    schema.startup(path)


def test_independent_route_publish_is_atomic_with_registry(tmp_path, monkeypatch):
    from execution.test_chat_route_bindings import _publish, _store

    from aiops_diagnostics.execution.chat_route_bindings import RouteBindingError

    store = _store(tmp_path)
    first = _publish(store, source_agent_name="source")
    with gateway_db.connection(store.path) as db:
        assert db.execute("SELECT agent_name FROM dify_app_registry").fetchone()[0] == "source"
    with pytest.raises(RouteBindingError):
        _publish(store, source_agent_name="wrong", expected_revision=0)
    before = snapshot(store.path)
    original = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql.startswith("INSERT INTO dify_app_registry"):
                raise RuntimeError("registry failure")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Failing, **kw))
        with pytest.raises(RuntimeError, match="registry failure"):
            _publish(store, source_agent_name="next", expected_revision=first.revision)
    assert snapshot(store.path) == before


def test_final_validation_sees_old_version_until_all_shapes_are_complete(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    historical(path)
    original = schema._validate
    observed = []

    def validate(conn, *, legacy):
        if not legacy:
            observed.append(conn.execute("PRAGMA user_version").fetchone()[0])
        return original(conn, legacy=legacy)

    monkeypatch.setattr(schema, "_validate", validate)
    schema.ensure(path)
    assert observed == [0]


def test_settings_validate_rejects_drift_before_route_construction(tmp_path):
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    path = tmp_path / "gateway.db"
    schema.ensure(path)
    with gateway_db.connection(path, write=True) as db:
        db.execute("ALTER TABLE agent_runs DROP COLUMN trace_json")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=path,
        server_config_file=tmp_path / "unused",
        chat_route_bindings_file=str(path),
    )
    with pytest.raises(schema.SchemaError):
        settings.validate()


def test_mapping_is_independent(tmp_path):
    from aiops_diagnostics.dify.dify_login_service import MappingStore

    path = tmp_path / "mappings.db"
    MappingStore(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        } == {
            "dify_login_mappings",
            "dify_login_audit",
        }
    assert not (tmp_path / "gateway.db").exists()


def test_wal_busy_has_five_second_deadline(tmp_path, monkeypatch):
    original = sqlite3.connect

    class AlwaysBusy(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA journal_mode=WAL":
                exc = sqlite3.OperationalError("synthetic busy")
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise exc
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=AlwaysBusy, **kw))
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="synthetic busy"):
        schema.ensure(tmp_path / "gateway.db")
    assert 4.9 <= time.monotonic() - start < 6


def test_scope_json_missing_uses_only_the_historical_append_rule(tmp_path):
    # 9080a463 自带的旧库补列规则明确允许 scope_json 缺失，默认空串，不猜身份。
    path = tmp_path / "gateway.db"
    ddl = LEGACY_DIFY.replace("scope_json TEXT NOT NULL, ", "")
    with closing(sqlite3.connect(path)) as db:
        db.execute(ddl)
    schema.ensure(path)
    with gateway_db.connection(path) as db:
        assert schema._columns(db, "chat_dify_runs")["scope_json"][2] == "''"
    assert schema.check(path)["warnings"] == ["legacy default: chat_dify_runs.scope_json = ''"]


def test_index_name_collision_on_an_extra_table_is_refused_by_check(tmp_path):
    path = tmp_path / "gateway.db"
    historical(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE INDEX idx_agent_runs_qa ON historic_notes(note)")
    before = snapshot(path)
    with pytest.raises(schema.SchemaError, match="index name collision"):
        schema.check(path)
    assert snapshot(path) == before


def test_sqlitex_prefix_is_not_mistaken_for_an_empty_database(tmp_path):
    path = tmp_path / "wrong.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sqlitex_business_data (value TEXT)")
    before = snapshot(path)
    with pytest.raises(schema.SchemaError, match="not a gateway database"):
        schema.ensure(path)
    assert snapshot(path) == before


def test_node_record_uses_named_columns_when_physical_order_differs(tmp_path):
    from types import SimpleNamespace

    from aiops_diagnostics.execution import node_capabilities

    path = tmp_path / "gateway.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("""CREATE TABLE execution_node_capabilities (
            node_id TEXT NOT NULL, environment TEXT NOT NULL, body TEXT NOT NULL,
            observed_at REAL NOT NULL, PRIMARY KEY(environment,node_id))""")
    schema.startup(path)
    config = SimpleNamespace(environment="env-a")
    node = SimpleNamespace(node_id="node-a")
    for generation in (1, 2):
        payload = {"generation": generation, "schemas": ["synthetic/1"]}
        node_capabilities.record(path, config, node, payload)
        with gateway_db.connection(path) as db:
            row = db.execute(
                "SELECT body FROM execution_node_capabilities WHERE environment=? AND node_id=?",
                ("env-a", "node-a"),
            ).fetchone()
            assert row is not None
            assert json.loads(row[0]) == payload
            assert db.execute("SELECT count(*) FROM execution_node_capabilities").fetchone()[0] == 1


def test_legacy_cleanup_rechecks_after_downgrade_and_clean_startup_is_readonly(tmp_path, monkeypatch):
    from aiops_diagnostics.dify.service_keys import KeyOperations

    path = tmp_path / "gateway.db"
    ops = KeyOperations(path)
    traces = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(traces.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    ops.clean_legacy()
    assert not any(sql.startswith(("BEGIN", "UPDATE")) for sql in traces)
    for value in ("old-synthetic", "written-after-downgrade"):
        with gateway_db.connection(path, write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO service_key_ops VALUES(?,?,?,?,?)",
                ("app", "workspace", json.dumps({value: "id"}), "creating", 1),
            )
        assert json.loads(KeyOperations(path).get("app")["before_json"]) == ["id"]
        traces.clear()
        ops.clean_legacy()
        assert "PRAGMA secure_delete=ON" in traces
        with gateway_db.connection(path) as db:
            assert json.loads(db.execute("SELECT before_json FROM service_key_ops").fetchone()[0]) == ["id"]
