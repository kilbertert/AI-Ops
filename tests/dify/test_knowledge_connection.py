from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError
from aiops_diagnostics.dify.knowledge_connection import ensure


def settings():
    return SimpleNamespace(
        public_base_url="https://gateway.example",
        environment="41",
        dify_external_knowledge_api_id="old-api",
        dify_knowledge_api_key="private-key",
    )


def test_each_workspace_gets_own_api_and_retry_reuses_created_connection():
    rows = {
        "A": [
            {
                "id": "old-api",
                "tenant_id": "A",
                "name": "legacy",
                "settings": {"endpoint": "https://gateway.example/v1/dify"},
            }
        ],
        "B": [],
    }
    calls = []

    def call(ws, path, method="GET", body=None):
        if path.endswith("/old-api"):
            if ws == "A":
                return rows["A"][0]
            raise ConsoleApiError("NOT_FOUND", "missing", status=400)
        if method == "GET":
            return {"data": rows[ws], "has_more": False}
        assert body["settings"]["api_key"] == "private-key"
        calls.append(ws)
        row = {"id": "api-" + ws, "tenant_id": ws, **body}
        rows[ws].append(row)
        return row

    console = SimpleNamespace(_call=call)
    assert ensure(console, "A", settings()) == "old-api"
    assert ensure(console, "B", settings()) == "api-B"
    assert ensure(console, "B", settings()) == "api-B"
    assert calls == ["B"]


def test_wrong_workspace_connection_is_rejected():
    console = SimpleNamespace(
        _call=lambda *a, **kw: {
            "data": [
                {
                    "id": "old-api",
                    "tenant_id": "FOREIGN",
                    "name": "legacy",
                    "settings": {"endpoint": "https://gateway.example/v1/dify"},
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        ensure(console, "B", settings())


def test_sync_uses_current_workspace_api(tmp_path, monkeypatch):
    from knowledge.test_knowledge_sync_by_workspace import (
        WORKSPACE,
        _client,
        _headers,
        _insert_workspace,
        _KBClient,
    )

    from aiops_diagnostics.dify import dify_knowledge_datasets, knowledge_connection

    seen = []
    monkeypatch.setattr(
        knowledge_connection, "ensure", lambda console, ws, settings: "own-api" if ws == WORKSPACE else None
    )
    monkeypatch.setattr(dify_knowledge_datasets, "run", lambda **kw: seen.append(kw) or {"applied": []})
    client, workspaces = _client(tmp_path, kb=_KBClient([]))
    _insert_workspace(workspaces)
    assert (
        client.post(
            "/v1/internal/knowledge/sync", params={"workspace": WORKSPACE}, headers=_headers()
        ).status_code
        == 200
    )
    assert seen[0]["external_knowledge_api_id"] == "own-api"


def test_created_connection_is_recovered_after_lost_response():
    rows = []

    def call(ws, path, method="GET", body=None):
        if path.endswith("/old-api"):
            raise ConsoleApiError("NOT_FOUND", "missing", status=400)
        if method == "GET":
            return {"data": rows, "has_more": False}
        rows.append({"id": "new", "tenant_id": ws, **body})
        raise TimeoutError("lost create response")

    console = SimpleNamespace(_call=call)
    with pytest.raises(TimeoutError):
        ensure(console, "B", settings())
    assert ensure(console, "B", settings()) == "new"
    assert len(rows) == 1
