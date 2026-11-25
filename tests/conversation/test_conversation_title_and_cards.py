"""#1706 行为契约测试：历史会话自动标题、PATCH 重命名、agent_id 过滤与完整轮次卡片回读。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.conversation.conversation_store import ConversationStore
from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from conversation.test_conversation_api import _Authorizer, _Caller, _Runtime

_OP = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
_OP_OTHER = {"Authorization": "Bearer other-tenant", "X-Business-Entry": "operator"}
_CONSUMER = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


class _MultiTenantDirectory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(f"B-{c_user_id}", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _test_client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=_Authorizer(),
        platform_resolver=PlatformIdentityResolver(_MultiTenantDirectory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def test_auto_title_rename_and_full_turn_card_readback(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        created = client.post(
            "/v1/conversations",
            headers=_OP,
            json={"agent_version_key": "agt_1234567890abcdef#v1"},
        )
        assert created.status_code == 201
        conv = created.json()
        conv_id = conv["conversation_id"]
        assert conv["title"] == ""
        assert conv["agent_id"] == "operator_main"
        assert conv["turn_count"] == 0

        store = ConversationStore(tmp_path / "gateway.db")
        scope_fp = _scope_fp_of(tmp_path / "gateway.db", conv_id)
        long_q = "请帮我排查南山科技园东区快充站 03 号桩近 7 天连续离线并伴随订单异常停止的具体原因"
        turn_no = store.begin_turn(conv_id, scope_fp, kind="qa", question=long_q)
        store.complete_turn(
            conv_id,
            scope_fp,
            turn_no,
            answer={"blocks": [{"kind": "text", "text": "已核对离线记录。"}]},
            steps=[
                {
                    "step_id": "st_1",
                    "node_type": "tool",
                    "node_code": "cpms_device_offline",
                    "title": "排查设备离线记录",
                    "status": "succeeded",
                    "elapsed_ms": 210,
                }
            ],
            citations=[
                {
                    "citation_id": "cit_1",
                    "source_type": "cpms_tool",
                    "title": "设备离线记录与订单关联",
                    "downloadable": False,
                }
            ],
            elapsed_ms=850,
            delivery_mode="diagnosis",
        )

        listed = client.get("/v1/conversations", params={"agent_id": "operator_main"}, headers=_OP)
        assert listed.status_code == 200
        items = listed.json()["conversations"]
        assert len(items) == 1
        assert items[0]["conversation_id"] == conv_id
        assert 1 <= len(items[0]["title"]) <= 40
        assert items[0]["title"].endswith("…")
        assert items[0]["turn_count"] == 1

        bad_filter = client.get("/v1/conversations", params={"agent_id": "nonexistent"}, headers=_OP)
        assert bad_filter.status_code == 404
        assert bad_filter.json()["error"]["code"] == "AGENT_NOT_FOUND"

        renamed = client.patch(
            f"/v1/conversations/{conv_id}",
            headers=_OP,
            json={"title": "南山站 03 桩离线复盘"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "南山站 03 桩离线复盘"

        assert (
            client.patch(f"/v1/conversations/{conv_id}", headers=_OP, json={"title": "   "}).status_code
            == 422
        )
        assert (
            client.patch(f"/v1/conversations/{conv_id}", headers=_OP, json={"title": "x" * 81}).status_code
            == 422
        )

        # Cross-tenant & cross-entry isolation return 404
        assert (
            client.patch(
                f"/v1/conversations/{conv_id}", headers=_OP_OTHER, json={"title": "越权"}
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"/v1/conversations/{conv_id}", headers=_CONSUMER, json={"title": "跨入口"}
            ).status_code
            == 404
        )

        detail = client.get(f"/v1/conversations/{conv_id}", headers=_OP)
        assert detail.status_code == 200
        body = detail.json()
        assert body["title"] == "南山站 03 桩离线复盘"
        assert body["turn_count"] == 1
        assert len(body["turns"]) == 1
        turn = body["turns"][0]
        assert turn["turn_no"] == 1
        assert turn["attempt_no"] == 1
        assert turn["attachments"] == []
        assert turn["delivery_mode"] == "diagnosis"
        assert turn["elapsed_ms"] == 850
        assert turn["steps"][0]["node_code"] == "cpms_device_offline"
        assert turn["citations"][0]["citation_id"] == "cit_1"
        assert turn["feedback"] is None


def test_legacy_turn_rows_migrate_cleanly_to_v3_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    gateway_schema.ensure(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            "ALTER TABLE conversations DROP COLUMN title;"
            "ALTER TABLE conversation_turns DROP COLUMN attempt_no;"
            "ALTER TABLE conversation_turns DROP COLUMN delivery_mode;"
            "INSERT INTO conversations(conversation_id,scope_fingerprint,business_entry,"
            "agent_version_key,created_at,updated_at,expires_at) "
            "VALUES('conv_oldturn01','fp_old','operator','agt_12345678#v1',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2099-01-01T00:00:00+00:00');"
            "INSERT INTO conversation_turns(turn_id,conversation_id,turn_no,kind,question,"
            "answer_json,token_count,created_at) "
            "VALUES('turn_old1','conv_oldturn01',1,'qa','旧问题',"
            '\'{"blocks":[{"kind":"text","text":"旧回答"}]}\',10,\'2026-01-01T00:00:01+00:00\');'
            "PRAGMA user_version=2;"
        )
    gateway_schema.ensure(db_path)
    store = ConversationStore(db_path)
    conv = store.get("conv_oldturn01", "fp_old")
    assert conv["title"] == ""
    assert conv["turn_count"] == 1
    turns = store.turns("conv_oldturn01", "fp_old")
    assert len(turns) == 1
    assert turns[0]["attempt_no"] == 1
    assert turns[0]["steps"] == []
    assert turns[0]["citations"] == []
    assert turns[0]["attachment_ids"] == []
    assert turns[0]["delivery_mode"] is None


def _scope_fp_of(db_path: Path, conv_id: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        return str(
            conn.execute(
                "SELECT scope_fingerprint FROM conversations WHERE conversation_id = ?", (conv_id,)
            ).fetchone()[0]
        )
