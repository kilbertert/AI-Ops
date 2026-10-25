"""原图片输入只绑定本人这一版，规范化后才发Dify。"""

import io
import json
import time
import uuid

import pytest
from PIL import Image
from test_app_runtime_dify_entry import HEADERS, dify_chat
from test_app_runtime_node_entry import TOKENS
from test_consumer_chat_service import Client, done
from test_consumer_knowledge_flow import setup

from aiops_diagnostics.consumer_photo import PhotoStore, normalize_photo
from aiops_diagnostics.dify_chatflow import ChatflowReply


def photo():
    out = io.BytesIO()
    image = Image.new("RGB", (48, 32), "red")
    exif = Image.Exif()
    exif[270] = "private note"
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


class PhotoFlow(Client):
    env = None
    uploads = []
    readable = True
    photo_id = str(uuid.uuid4())

    def upload_photo(self, **kwargs):
        self.uploads.append(kwargs)
        return self.photo_id

    def run(self, **kwargs):
        self.calls.append(kwargs)
        ids = [str(uuid.uuid4()) for _ in range(4)]
        kwargs["on_started"](*ids)
        has_image = bool(kwargs.get("photo_id"))
        if has_image:
            assert kwargs["photo_id"] == self.photo_id
            assert kwargs["inputs"]["has_photo"] == "yes"
        r = self.env["client"].post(
            "/v1/internal/consumer-knowledge",
            json={
                "image_text": json.dumps(
                    {"readable": self.readable, "text": "Startup failed." if self.readable else ""}
                )
                if has_image
                else "no"
            },
            headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
        )
        assert r.status_code == 200, r.text
        return ChatflowReply(*ids, json.dumps({"kind": "knowledge", "faq_id": "KB-C-007"}), {})


def prepare(env, monkeypatch, tmp_path):
    runtime, kb, _ = setup(env, monkeypatch, tmp_path)
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0]["photo_enabled"] = True
    env["bindings"].write_text(json.dumps(doc))
    PhotoFlow.env = env
    PhotoFlow.uploads = []
    PhotoFlow.readable = True
    monkeypatch.setattr("aiops_diagnostics.consumer_chat_service.DifyChatflowClient", PhotoFlow)
    return runtime, kb


def upload(env, headers=HEADERS):
    return env["client"].post(
        "/v1/assistant/photos", headers={**headers, "Content-Type": "image/jpeg"}, content=photo()
    )


def ask(env, photo_id, headers=HEADERS, **extra):
    return env["client"].post(
        "/v1/assistant/questions",
        headers=headers,
        json={"question": "图片上的提示怎么办？", "photo_id": photo_id, **extra},
    )


def test_photo_normalization_strips_metadata_and_rejects_bad_inputs():
    clean = normalize_photo(photo(), "image/jpeg")
    with Image.open(io.BytesIO(clean)) as image:
        assert image.size == (48, 32) and not image.getexif()
    for raw, mime in [
        (b"<svg></svg>", "image/png"),
        (photo(), "text/html"),
        (b"0" * (4 * 1024 * 1024 + 1), "image/jpeg"),
    ]:
        with pytest.raises(ValueError):
            normalize_photo(raw, mime)


def test_upload_is_owned_one_time_and_model_receives_no_private_metadata(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb = prepare(env, monkeypatch, tmp_path)
        uploaded = upload(env)
        assert uploaded.status_code == 201, uploaded.text
        key = uploaded.json()["photo_id"]
        monkeypatch.setitem(TOKENS, "token-b", ("USER-B", "TENANT-A"))
        assert ask(env, key, {**HEADERS, "Authorization": "Bearer token-b"}).status_code == 404
        first = ask(env, key)
        assert first.status_code == 202, first.text
        assert done(env, first.json()["qa_id"]).json()["status"] == "completed"
        assert len(PhotoFlow.uploads) == 1
        with Image.open(io.BytesIO(PhotoFlow.uploads[0]["image"])) as image:
            assert not image.getexif()
        assert "Startup failed." in kb.calls[0][1]
        assert PhotoFlow.uploads[0]["user"] == Client.calls[0]["user"]
        assert ask(env, key).status_code == 404
        result = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "怎么开始充电？", "conversation_id": first.json()["conversation_id"]},
        )
        assert done(env, result.json()["qa_id"]).json()["status"] == "completed"
        assert Client.calls[-1]["conversation_id"] == ""


def test_changed_binding_refuses_photo_and_limits_are_real(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = prepare(env, monkeypatch, tmp_path)
        one = upload(env).json()["photo_id"]
        assert upload(env).status_code == 201
        assert upload(env).status_code == 429
        d = json.loads(env["bindings"].read_text())
        d["bindings"][0]["agent_version_no"] += 1
        env["bindings"].write_text(json.dumps(d))
        assert ask(env, one).status_code == 404
        runtime.consumer_photos.close()
        assert not runtime.consumer_photos._items


def test_expired_photo_cannot_be_consumed():
    store = PhotoStore(ttl=0.01)
    try:
        key = store.put("subject", "binding", b"jpeg")
        time.sleep(0.02)
        from aiops_diagnostics.chat_execution import ChatRunUnavailable

        with pytest.raises(ChatRunUnavailable):
            store.take(key, "subject", "binding")
        assert not store._items
    finally:
        store.close()


def test_unreadable_photo_asks_for_clarification_without_retrieval(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb = prepare(env, monkeypatch, tmp_path)
        PhotoFlow.readable = False
        key = upload(env).json()["photo_id"]
        result = done(env, ask(env, key).json()["qa_id"]).json()
        assert result["status"] == "completed", result
        assert "不清晰" in result["result"]["blocks"][0]["text"]
        assert kb.calls == []


def test_claimed_photos_still_count_against_memory_limit():
    store = PhotoStore()
    try:
        first = store.put("subject", "binding", b"jpeg")
        store.take(first, "subject", "binding")
        store.put("subject", "binding", b"jpeg")
        from aiops_diagnostics.chat_execution import ChatRunUnavailable

        with pytest.raises(ChatRunUnavailable):
            store.put("subject", "binding", b"jpeg")
        store.finish(first)
        assert store.put("subject", "binding", b"jpeg")
    finally:
        store.close()


@pytest.mark.parametrize("end", ["cancel", "expire"])
def test_run_ended_during_dify_upload_does_not_start_model(tmp_path, monkeypatch, end):
    import sqlite3
    import threading

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = prepare(env, monkeypatch, tmp_path)
        entered, release = threading.Event(), threading.Event()

        def uploading(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return self.photo_id

        monkeypatch.setattr(PhotoFlow, "upload_photo", uploading)
        key = upload(env).json()["photo_id"]
        first = ask(env, key).json()
        assert entered.wait(5)
        future = runtime._futures[first["qa_id"]]
        if end == "cancel":
            env["client"].post("/v1/assistant/questions/" + first["qa_id"] + "/cancel", headers=HEADERS)
        else:
            with sqlite3.connect(env["db"]) as con:
                con.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
        release.set()
        future.result(timeout=5)
        result = done(env, first["qa_id"]).json()
        assert result["status"] == ("cancelled" if end == "cancel" else "failed")
        assert result.get("result") is None and Client.calls == []
        assert not runtime.consumer_photos._active


def test_upload_failure_releases_reservation_and_declared_mime_must_match(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = prepare(env, monkeypatch, tmp_path)
        for _ in range(3):
            response = env["client"].post(
                "/v1/assistant/photos", headers={**HEADERS, "Content-Type": "image/png"}, content=photo()
            )
            assert response.status_code == 400
        assert not runtime.consumer_photos._items and upload(env).status_code == 201


def test_dify_template_sends_visual_text_in_the_actual_body_field():
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(
        (Path(__file__).parents[1] / "deploy/dify-chat/consumer-service.dsl.yml").read_text()
    )
    node = next(n for n in doc["workflow"]["graph"]["nodes"] if n["id"] == "knowledge")
    body = node["data"]["body"]
    assert body["type"] == "raw-text"
    assert body["data"] == [{"type": "text", "value": "{{#photo_context.output#}}"}]
    assert "image_text" not in node["data"]["params"]
