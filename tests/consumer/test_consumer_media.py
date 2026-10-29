"""WF01已审核素材用原媒体端点真正交付，随QA/绑定撤销。"""

import json
import sqlite3

import pytest
from test_app_runtime_dify_entry import dify_chat

from aiops_diagnostics.consumer.consumer_knowledge import load_content
from consumer.test_consumer_chat_service import done
from consumer.test_consumer_knowledge_flow import ask, setup, wire

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
VIDEO = b"0000ftypisom" + b"test-video"


def prepare(env, monkeypatch, tmp_path):
    runtime, kb, path = setup(env, monkeypatch, tmp_path)
    doc = json.loads(path.read_text())
    doc["media"] = {
        "IMG-001": {
            "state": "approved",
            "kind": "image",
            "mime_type": "image/png",
            "knowledge_base_id": "kb-own",
            "document_id": "img-doc",
            "backend_id": "img-1",
            "title": "操作图",
        },
        "VID-001": {
            "state": "approved",
            "kind": "video",
            "mime_type": "video/mp4",
            "knowledge_base_id": "kb-own",
            "document_id": "video-doc",
            "backend_id": "video-doc",
            "title": "操作视频",
        },
    }
    doc["entries"]["KB-C-007"]["media_ids"] = ["IMG-001", "VID-001"]
    path.write_text(json.dumps(doc))
    kb.fetches = []
    kb.after_fetch = None

    def fetch(grant, **kwargs):
        kb.fetches.append((grant.knowledge_base_id, grant.document_id, grant.backend_id))
        if kb.after_fetch:
            kb.after_fetch()
        return PNG if grant.kind == "image" else VIDEO

    kb.fetch_media = fetch
    wire(env, monkeypatch)
    return runtime, kb, path


def test_approved_media_is_fetchable_and_ranges_work(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, _ = prepare(env, monkeypatch, tmp_path)
        qa = ask(env).json()["qa_id"]
        result = done(env, qa).json()
        assert result["status"] == "completed", result
        media = [b["media"] for b in result["result"]["blocks"] if b["kind"] in {"image", "video"}]
        assert len(media) == 2
        image = env["client"].get(media[0]["url"])
        assert image.status_code == 200 and image.content == PNG
        assert image.headers["content-type"] == "image/png"
        video = env["client"].get(media[1]["url"], headers={"Range": "bytes=0-3"})
        assert video.status_code == 206 and video.content == VIDEO[:4]
        assert "no-store" in video.headers["cache-control"]
        forged = env["client"].get(media[0]["url"] + "f")
        assert forged.status_code == 403 and len(kb.fetches) == 2


@pytest.mark.parametrize("change", ["expiry", "content", "disabled", "during_fetch"])
def test_media_is_refused_when_answer_is_no_longer_deliverable(tmp_path, monkeypatch, change):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, path = prepare(env, monkeypatch, tmp_path)
        qa = ask(env).json()["qa_id"]
        url = done(env, qa).json()["result"]["blocks"][2]["media"]["url"]
        if change == "expiry":
            with sqlite3.connect(env["db"]) as con:
                con.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
        elif change == "content":
            path.write_text(path.read_text() + "\n")
        elif change == "disabled":
            env["settings"].consumer_chatflow_enabled = False
        else:
            kb.after_fetch = lambda: path.write_text(path.read_text() + "\n")
        response = env["client"].get(url)
        assert response.status_code == 403 and response.content == b""
        assert len(kb.fetches) == (1 if change == "during_fetch" else 0)


def test_draft_unknown_or_unsafe_media_is_not_accepted(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, path = prepare(env, monkeypatch, tmp_path)
        original = json.loads(path.read_text())
        for field, value in [("state", "draft"), ("mime_type", "text/html"), ("backend_id", "")]:
            doc = json.loads(json.dumps(original))
            doc["media"]["IMG-001"][field] = value
            path.write_text(json.dumps(doc))
            with pytest.raises(ValueError):
                load_content(str(path), tenant="TENANT-A", environment="env-a")


def test_foreign_library_media_is_not_signed(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, path = prepare(env, monkeypatch, tmp_path)
        doc = json.loads(path.read_text())
        for media in doc["media"].values():
            media["knowledge_base_id"] = "kb-foreign"
        path.write_text(json.dumps(doc))
        result = done(env, ask(env).json()["qa_id"]).json()["result"]
        assert [b["kind"] for b in result["blocks"]] == ["text", "reference"]


def test_media_language_and_deleted_run_fail_closed(tmp_path, monkeypatch):
    from test_app_runtime_dify_entry import HEADERS

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, path = prepare(env, monkeypatch, tmp_path)
        doc = json.loads(path.read_text())
        doc["media"]["IMG-001"]["language"] = "zh"
        doc["media"]["VID-001"]["language"] = "en"
        path.write_text(json.dumps(doc))
        post = env["client"].post(
            "/v1/assistant/questions",
            headers={**HEADERS, "Accept-Language": "en"},
            json={"question": "How do I start charging?"},
        )
        result = done(env, post.json()["qa_id"]).json()["result"]
        media = [b for b in result["blocks"] if b["kind"] in {"image", "video"}]
        assert [b["kind"] for b in media] == ["video"]
        with sqlite3.connect(env["db"]) as con:
            con.execute("DELETE FROM chat_dify_runs")
        response = env["client"].get(media[0]["media"]["url"])
        assert response.status_code == 403 and not kb.fetches
