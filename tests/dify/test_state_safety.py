"""复现开通撤销、凭据持久化、发布窗口与失败日志的回归。"""

from types import SimpleNamespace

import pytest
from operations.test_operator_cpms import operator  # noqa: F401

from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants
from aiops_diagnostics.dify import self_service_api, service_keys
from dify.test_dual_entry import TARGET, Dsl, dual  # noqa: F401


def test_revoke_survives_startup_backfill(tmp_path):
    db = tmp_path / "gateway.db"
    context = SimpleNamespace(
        settings=SimpleNamespace(database_file=db, execution_environment="env-a"),
        agent_manager=SimpleNamespace(
            store=SimpleNamespace(
                version=lambda *a: SimpleNamespace(
                    snapshot={"consumer_service": {"knowledge_base_ids": ["private-again"]}}
                )
            )
        ),
    )
    service = SimpleNamespace(
        routes=SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    entry="consumer",
                    active=True,
                    engine="dify",
                    agent_id="a",
                    tenant_id="T",
                    agent_version_no=1,
                )
            ]
        )
    )
    self_service_api._backfill_knowledge_grants(context, service)
    grants = KnowledgeGrants(db)
    assert grants.granted("T", "env-a") == {"private-again"}
    grants.revoke("T", "env-a", "private-again")
    assert grants.granted("T", "env-a") == set()
    self_service_api._backfill_knowledge_grants(context, service)
    print("after-restart-grants", grants.granted("T", "env-a"))
    assert grants.granted("T", "env-a") == set(), "startup reauthorized a revoked internal library"


def test_key_operation_snapshot_contains_no_plaintext_key(tmp_path, monkeypatch):
    from dify.test_dual_entry import Console

    app = "11111111-1111-4111-8111-111111111111"
    console = Console()
    marker = "SYNTHETIC_EXISTING_APP_KEY_NOT_FOR_PERSISTENCE"
    console.keys[app] = {marker: "existing-key-id"}
    original = console._call
    created = [False]

    def fail_after_create(workspace, path, **kw):
        if path.endswith("/api-keys") and kw.get("method", "GET") == "GET" and created[0]:
            raise TimeoutError("synthetic list unavailable")
        result = original(workspace, path, **kw)
        if kw.get("method") == "POST":
            created[0] = True
            raise TimeoutError("synthetic lost response")
        return result

    monkeypatch.setattr(console, "_call", fail_after_create)
    db = tmp_path / "db"
    with pytest.raises(TimeoutError):
        service_keys.ensure(console, tmp_path / "keys", "w", app, database=db)
    import sqlite3

    with sqlite3.connect(db) as connection:
        raw = connection.execute("SELECT before_json FROM service_key_ops WHERE app_id=?", (app,)).fetchone()[
            0
        ]
    print("snapshot_contains_secret", marker in raw)
    assert marker not in raw, "key operation persisted an unrelated integration token in plaintext"


def test_legacy_key_journal_is_migrated_to_ids_only(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "db"
    ops = service_keys.KeyOperations(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO service_key_ops VALUES(?,?,?,?,?)",
            ("app", "ws", json.dumps({"SYNTHETIC_SECRET": "key-1"}), "creating", 1),
        )
    ops = service_keys.KeyOperations(path)
    assert json.loads(ops.get("app")["before_json"]) == ["key-1"]
    with sqlite3.connect(path) as db:
        assert isinstance(
            json.loads(db.execute("SELECT before_json FROM service_key_ops").fetchone()[0]), dict
        )
    ops.clean_legacy()
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT before_json FROM service_key_ops").fetchone()[0]) == ["key-1"]


def test_only_explicit_grant_can_reenable_revoked_knowledge(tmp_path):
    db = tmp_path / "db"
    g = KnowledgeGrants(db)
    g.grant("t", "env", "kb", "operator")
    assert g.revoke("t", "env", "kb")
    assert not g.revoke("t", "env", "kb")
    KnowledgeGrants(db).grant("t", "env", "kb", "startup", backfill=True)
    assert g.granted("t", "env") == set()
    g.grant("t", "env", "kb", "operator")
    assert g.granted("t", "env") == {"kb"}


@pytest.mark.parametrize("failure", ["readonly", "full"])
def test_key_storage_refusal_precedes_external_key_creation(tmp_path, monkeypatch, failure):
    import errno

    from dify.test_dual_entry import Console

    console = Console()
    real = service_keys.ensure_private_directory
    key_dir = tmp_path / "keys"

    def unavailable(path):
        if path == key_dir:
            raise OSError(errno.EROFS if failure == "readonly" else errno.ENOSPC, "synthetic storage refusal")
        return real(path)

    monkeypatch.setattr(service_keys, "ensure_private_directory", unavailable)
    with pytest.raises(service_keys.SetupRefused, match="SETUP_KEY_DIRECTORY_UNWRITABLE"):
        service_keys.ensure(
            console, key_dir, "w", "11111111-1111-4111-8111-111111111111", database=tmp_path / "db"
        )
    assert console.key_posts == 0
