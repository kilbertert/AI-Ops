"""#1448：真实历史表与故障注入保护初始化的持久化边界。"""

import sqlite3
from contextlib import closing

import pytest

from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.gateway.metrics_store import MetricsStore

# 原样取自9080a463:src/aiops_diagnostics/dify_run_store.py；不是从候选DDL删列生成。
LEGACY_DIFY = """
CREATE TABLE chat_dify_runs (
    run_id TEXT PRIMARY KEY, qa_id TEXT NOT NULL, scope_fingerprint TEXT NOT NULL,
    scope_json TEXT NOT NULL, binding_key TEXT NOT NULL DEFAULT '', order_no TEXT NOT NULL,
    environment TEXT NOT NULL, tenant_id TEXT NOT NULL, app_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL, workflow_run_id TEXT, task_id TEXT, grant_id TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    completed_at TEXT, snapshot_json TEXT
)
"""


def _snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name").fetchall()
        rows = {
            name: db.execute(f'SELECT * FROM "{name}"').fetchall()
            for kind, name, _, _ in schema
            if kind == "table"
        }
    return schema, rows


def test_historical_dify_migration_rolls_back_schema_and_rows(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(LEGACY_DIFY)
        db.execute(
            "INSERT INTO chat_dify_runs(run_id,qa_id,scope_fingerprint,scope_json,order_no,"
            "environment,tenant_id,app_id,workflow_id,grant_id,state,created_at,expires_at) "
            "VALUES('run','qa','scope','{}','order','test','tenant','app','workflow','grant',"
            "'issued','2026-01-01','2026-01-02')"
        )
    before = _snapshot(path)
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "ADD COLUMN conversation_id" in sql:
                raise RuntimeError("injected execute failure")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=FailingConnection, **kw))
        with pytest.raises(RuntimeError, match="injected execute failure"):
            DifyRunStore(path)
    assert _snapshot(path) == before
    DifyRunStore(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT run_id,scope_json,kind,conversation_id FROM chat_dify_runs").fetchone() == (
            "run",
            "{}",
            "chat",
            "",
        )


@pytest.mark.parametrize(
    ("store", "action", "target"),
    [
        (GatewayStore, sqlite3.SQLITE_CREATE_TABLE, "standard_diagnoses"),
        (MetricsStore, sqlite3.SQLITE_CREATE_INDEX, "idx_agent_metrics_agent"),
        (KnowledgeGrants, sqlite3.SQLITE_ALTER_TABLE, "consumer_knowledge_grants"),
        (ChatRouteBindingStore, sqlite3.SQLITE_ALTER_TABLE, "chat_route_bindings"),
    ],
)
def test_initializer_failure_leaves_no_partial_schema(tmp_path, monkeypatch, store, action, target):
    path = tmp_path / "gateway.db"
    before = _snapshot(path)
    connect = sqlite3.connect

    def deny_selected_ddl(op, first, second, _database, _trigger):
        return sqlite3.SQLITE_DENY if op == action and target in (first, second) else sqlite3.SQLITE_OK

    def failing_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(deny_selected_ddl)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", failing_connect)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store(path)
    assert _snapshot(path) == before
    store(path)
