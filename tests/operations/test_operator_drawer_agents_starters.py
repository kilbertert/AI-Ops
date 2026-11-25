"""#1705 行为契约测试：后台主智能体声明、operator 专属预设问题与 agent_id 预留。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from conversation.test_faq_gateway_api import _client

from aiops_diagnostics.common.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.publication.agent_content import (
    AGENT_STARTERS,
    OPERATOR_MAIN_AGENT_ID,
    OPERATOR_STARTERS,
    operator_starter_gaps,
)

_OPERATOR_HEADERS = {"Authorization": "Bearer operator-user", "X-Business-Entry": "operator"}
_CONSUMER_HEADERS = {"Authorization": "Bearer consumer-user", "X-Business-Entry": "consumer"}


def test_operator_starters_cover_every_supported_language() -> None:
    assert operator_starter_gaps() == ()


def test_assistant_agents_endpoint_returns_operator_main_and_rejects_consumer(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/v1/assistant/agents", headers=_OPERATOR_HEADERS)
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        body = resp.json()
        assert body["type"] == "assistant_agent_list"
        assert body["default_agent_id"] == OPERATOR_MAIN_AGENT_ID
        assert len(body["agents"]) == 1
        agent = body["agents"][0]
        assert agent["agent_id"] == OPERATOR_MAIN_AGENT_ID
        assert agent["is_default"] is True
        assert agent["capabilities"] == {
            "streaming": True,
            "node_path": True,
            "max_attachments": 5,
            "max_image_bytes": 10 * 1024 * 1024,
            "max_document_bytes": 20 * 1024 * 1024,
            "allowed_extensions": ["jpg", "jpeg", "png", "webp", "pdf", "doc", "docx"],
        }

        en = client.get(
            "/v1/assistant/agents",
            headers={**_OPERATOR_HEADERS, "Accept-Language": "en"},
        )
        assert en.status_code == 200
        assert en.json()["agents"][0]["name"] != agent["name"]

        consumer = client.get("/v1/assistant/agents", headers=_CONSUMER_HEADERS)
        assert consumer.status_code == 403
        assert consumer.json()["error"]["code"] == "PLATFORM_FORBIDDEN"

        anon = client.get("/v1/assistant/agents")
        assert anon.status_code == 401


def test_operator_starters_isolated_from_consumer_and_validates_agent_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        op_zh = client.get("/v1/assistant/starters", headers=_OPERATOR_HEADERS)
        assert op_zh.status_code == 200
        body = op_zh.json()
        assert body["type"] == "assistant_starters"
        assert body["agent_id"] == OPERATOR_MAIN_AGENT_ID
        assert body["greeting"] == OPERATOR_STARTERS.greeting["zh"]
        assert body["opening"] == body["greeting"]
        assert body["supporting_text"] == OPERATOR_STARTERS.supporting_text["zh"]
        assert body["disclaimer"] == OPERATOR_STARTERS.disclaimer["zh"]
        assert len(body["suggested_questions"]) == 4
        assert all(
            isinstance(item, dict) and item["id"].startswith("op_q_") and item["text"]
            for item in body["suggested_questions"]
        )

        for lang in SUPPORTED_LANGUAGES:
            localized = client.get(
                "/v1/assistant/starters",
                headers={**_OPERATOR_HEADERS, "Accept-Language": lang},
            )
            assert localized.status_code == 200
            assert localized.json()["language"] == lang
            assert localized.json()["greeting"] == OPERATOR_STARTERS.greeting[lang]

        # Consumer entry retains legacy string list shape untouched.
        c_zh = client.get("/v1/assistant/starters", headers=_CONSUMER_HEADERS)
        assert c_zh.status_code == 200
        assert c_zh.json()["opening"] == AGENT_STARTERS.opening["zh"]
        assert c_zh.json()["suggested_questions"] == list(AGENT_STARTERS.suggested["zh"])

        # Unknown agent_id rejected cleanly on both starters and questions.
        bad_starter = client.get(
            "/v1/assistant/starters",
            params={"agent_id": "unknown_agent"},
            headers=_OPERATOR_HEADERS,
        )
        assert bad_starter.status_code == 404
        assert bad_starter.json()["error"]["code"] == "AGENT_NOT_FOUND"

        bad_q = client.post(
            "/v1/assistant/questions",
            headers=_OPERATOR_HEADERS,
            json={"question": "排查离线充电桩", "agent_id": "unknown_agent"},
        )
        assert bad_q.status_code == 404
        assert bad_q.json()["error"]["code"] == "AGENT_NOT_FOUND"

        bad_c_q = client.post(
            "/v1/assistant/questions",
            headers=_CONSUMER_HEADERS,
            json={"question": "查订单", "agent_id": "operator_main"},
        )
        assert bad_c_q.status_code == 404
        assert bad_c_q.json()["error"]["code"] == "AGENT_NOT_FOUND"


def test_schema_v2_migrates_idempotently_to_v3_with_agent_id_default(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    gateway_schema.ensure(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            "ALTER TABLE conversations DROP COLUMN agent_id;"
            "ALTER TABLE assistant_questions DROP COLUMN agent_id;"
            "INSERT INTO conversations(conversation_id,scope_fingerprint,business_entry,"
            "agent_version_key,created_at,updated_at,expires_at) "
            "VALUES('conv_legacy01','fp1','operator','agt_12345678#v1',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00');"
            "PRAGMA user_version=2;"
        )
    report = gateway_schema.check(db_path)
    assert report["version"] == 2
    assert report["current_version"] == gateway_schema.VERSION
    gateway_schema.ensure(db_path)
    assert gateway_schema.check(db_path)["version"] == gateway_schema.VERSION
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT agent_id FROM conversations WHERE conversation_id='conv_legacy01'"
        ).fetchone()
    assert row == (OPERATOR_MAIN_AGENT_ID,)
