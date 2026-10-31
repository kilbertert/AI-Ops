"""复现开通撤销、凭据持久化、发布窗口与失败日志的回归。"""

from types import SimpleNamespace

import pytest
from operations.test_operator_cpms import operator  # noqa: F401

from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
from aiops_diagnostics.dify import self_service_api, service_keys
from aiops_diagnostics.dify import self_service_prepare as prepare
from dify.test_dual_entry import TARGET, Dsl, dual, request, run  # noqa: F401


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


def test_published_content_cannot_change_after_digest_check(dual, monkeypatch):  # noqa: F811
    from aiops_diagnostics.consumer.consumer_publication import workflow_of

    only = {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}
    result = run(dual, request(dual, scope="consumer", sources=only, key="r2-digest-window-001"))
    app = result["entries"]["consumer"]["app_id"]
    status = dual.service.status(dual.caller, app)
    doc = dual.console.apps[app]
    original = Dsl.export_dsl
    calls = [0]

    def changing(self, app_id=None, **kw):
        target = app_id or self.source.app_id
        if target == app and not kw.get("workflow_id"):
            calls[0] += 1
            if calls[0] == 2:
                for node in doc["workflow"]["graph"]["nodes"]:
                    if node["data"].get("type") == "llm":
                        template = node["data"].get("prompt_template", [])
                        if template:
                            template[0]["text"] += " SYNTHETIC_UNCONFIRMED_EDIT"
                        break
        return original(self, app_id, **kw)

    monkeypatch.setattr(Dsl, "export_dsl", changing)
    try:
        dual.service.publish(dual.caller, app, status["route_revision"], status["draft_sha256"])
    except Exception as exc:
        assert "DRAFT" in str(exc) or "changed" in str(exc).lower()
        return
    route = dual.service.route_for(TARGET, "consumer")
    frozen = dual.context.agent_manager.store.version(route.agent_id, TARGET, route.agent_version_no).snapshot
    print("published-matches-confirmed", workflow_of(frozen["workflow"]) == status["draft_sha256"])
    assert workflow_of(frozen["workflow"]) == status["draft_sha256"], "unconfirmed edit was published"


def test_step_exception_does_not_print_sensitive_details(dual, monkeypatch, capsys):  # noqa: F811
    marker = "SYNTHETIC_UPSTREAM_CREDENTIAL_SHOULD_NOT_LOG"
    monkeypatch.setattr(
        prepare, "consumer_plugin", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(marker))
    )
    only = {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}
    result = run(dual, request(dual, scope="consumer", sources=only, key="r2-log-safety-001"))
    assert result["state"] == "failed"
    captured = capsys.readouterr()
    print("sensitive_detail_logged", marker in captured.err)
    assert marker not in captured.err, "raw upstream exception printed into service log"


def test_lost_knowledge_create_response_does_not_create_second_library(dual, monkeypatch):  # noqa: F811
    original = dual.kb.create_knowledge_base
    original_list = dual.kb.list_knowledge_bases
    lose = [True]
    list_unavailable = [False]

    def created_but_lost(*a, **kw):
        result = original(*a, **kw)
        if lose[0]:
            lose[0] = False
            list_unavailable[0] = True
            raise TimeoutError("synthetic dataset creation reply lost")
        return result

    def list_after_create():
        if list_unavailable[0]:
            list_unavailable[0] = False
            raise TimeoutError("synthetic dataset list unavailable")
        return original_list()

    monkeypatch.setattr(dual.kb, "create_knowledge_base", created_but_lost)
    monkeypatch.setattr(dual.kb, "list_knowledge_bases", list_after_create)
    only = {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}
    job = dual.service.create(
        dual.caller, request(dual, scope="consumer", sources=only, key="r2-knowledge-loss-000001")
    )
    dual.service.run(job["id"], "session")
    assert len(dual.kb.created) == 1
    dual.service.run(job["id"], "session")
    print("knowledge-datasets-created", len(dual.kb.created))
    assert len(dual.kb.created) == 1, "lost response caused duplicate knowledge library"


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


@pytest.mark.parametrize("failure", ["consumer_endpoint", "consumer_plugin", "consumer_models"])
def test_dependencies_fail_before_any_customer_library_is_created(dual, monkeypatch, failure):  # noqa: F811
    original = getattr(prepare, failure)

    def unavailable(*args, **kwargs):
        raise prepare.SetupRefused("SETUP_STEP_FAILED")

    monkeypatch.setattr(prepare, failure, unavailable)
    only = {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}
    for index in range(2):
        job = dual.service.create(
            dual.caller, request(dual, scope="consumer", sources=only, key=f"dependency-first-{index:03}")
        )
        for _ in range(2):
            dual.service.run(job["id"], "session")
            assert dual.service.store.get(job["id"])["state"] == "failed"
        assert dual.kb.created == [], "failed setup created an unusable customer library"
    monkeypatch.setattr(prepare, failure, original)
    dual.service.run(job["id"], "session")
    assert dual.service.store.get(job["id"])["state"] == "completed"
    assert len(dual.kb.created) == 1


def test_same_job_resumes_after_app_import_failure_without_creating_another_library(dual, monkeypatch):  # noqa: F811
    real = prepare.copy_consumer_app
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("synthetic import unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(prepare, "copy_consumer_app", fail_once)
    only = {"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}}
    job = dual.service.create(
        dual.caller, request(dual, scope="consumer", sources=only, key="import-resume-library-001")
    )
    dual.service.run(job["id"], "session")
    assert dual.service.store.get(job["id"])["state"] == "failed"
    assert len(dual.kb.created) == 1
    dual.service.run(job["id"], "session")
    assert dual.service.store.get(job["id"])["state"] == "completed"
    assert len(dual.kb.created) == 1
