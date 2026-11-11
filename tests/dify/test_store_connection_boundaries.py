"""已证实的纯读写锁与连接未关闭回归。"""

import sqlite3
from contextlib import closing

import pytest

from aiops_diagnostics.dify.dify_login_service import MappingStore
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway import gateway_db


@pytest.mark.parametrize("method", ["request", "get", "recent", "app", "apps", "consumer_target"])
def test_self_service_pure_reads_while_another_writer_holds_lease(tmp_path, method):
    path = tmp_path / "gateway.db"
    store = SelfServiceStore(path)
    with store.connection() as db:
        db.execute(
            "INSERT INTO operator_apps(app_id,tenant_id,workspace_id,agent_name,profile_id,created_by,"
            "entry,environment) VALUES('app','tenant','workspace','agent','profile','actor',"
            "'consumer','test')"
        )
        db.execute(
            "INSERT INTO operator_setup_jobs(id,actor,request_key,request_json,state,step,updated_at) "
            "VALUES('job','actor','request','{}','pending','workspace',0)"
        )
    args = {
        "request": ("actor", "request"),
        "get": ("job",),
        "recent": ("actor",),
        "app": ("app",),
        "apps": (),
        "consumer_target": ("tenant", "test", "app"),
    }
    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = getattr(store, method)(*args[method])
            row = result[0] if isinstance(result, list) else result
            assert row["app_id" if method in {"app", "apps", "consumer_target"} else "id"] == (
                "app" if method in {"app", "apps", "consumer_target"} else "job"
            )
        finally:
            holder.rollback()


@pytest.mark.parametrize("store_class", [MappingStore, ChatRouteBindingStore])
def test_store_connections_close_without_gc(tmp_path, monkeypatch, store_class):
    connections = []
    connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)  # 保留引用，不能靠CPython引用计数或GC替调用方关闭。
        return db

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    store = store_class(tmp_path / "store.db")
    for _ in range(30):
        if store_class is MappingStore:
            assert store.get("missing") is None
        else:
            assert store.all() == ()
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_mapping_connection_commits_rolls_back_and_closes(tmp_path):
    store = MappingStore(tmp_path / "mappings.db")
    with store._connect() as db:
        db.execute("INSERT INTO dify_login_audit(at,company_user_id,action) VALUES('now','user','login')")
    with pytest.raises(RuntimeError), store._connect() as failed:
        failed.execute("DELETE FROM dify_login_audit")
        raise RuntimeError("rollback")
    with closing(sqlite3.connect(store.path)) as reader:
        assert reader.execute("SELECT count(*) FROM dify_login_audit").fetchone()[0] == 1
    for connection in (db, failed):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("pragma", ["foreign_keys", "busy_timeout", "journal_mode"])
def test_gateway_configuration_failure_closes_connection(tmp_path, monkeypatch, pragma):
    connections = []
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if pragma in sql:
                raise RuntimeError("injected PRAGMA failure")
            return super().execute(sql, parameters)

    def failing_connect(*args, **kwargs):
        db = connect(*args, factory=FailingConnection, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="injected PRAGMA failure"), gateway_db.initialize(tmp_path / "db"):
        pytest.fail("configuration must fail before yielding")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
