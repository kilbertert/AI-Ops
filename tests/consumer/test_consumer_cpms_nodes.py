"""独立节点必须决定实际业务调用；错误能力连缓存也不释放。"""

import json
import uuid

import pytest
from test_app_runtime_dify_entry import dify_chat
from test_consumer_chat_service import Client, done
from test_consumer_live_context import ask, wire

from aiops_diagnostics.dify_chatflow import ChatflowReply


def split(env):
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0]["callback_version"] = 2
    env["bindings"].write_text(json.dumps(doc))


@pytest.mark.parametrize(
    "question,capability", [("我上一个订单现在是什么情况", "status"), ("怎么开始充电？", "knowledge")]
)
def test_real_callback_is_selected_by_frozen_capability(tmp_path, monkeypatch, question, capability):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        split(env)
        monkeypatch.setattr(
            runtime,
            "classify_consumer",
            lambda *a, **k: {
                "intent": "order_status" if capability == "status" else "knowledge",
                "order_reference": "latest" if capability == "status" else "none",
                "uses_context": False,
            },
        )

        class Flow(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                assert kwargs["inputs"]["data_kind"] == capability
                headers = {"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]}
                paths = ["/v1/internal/consumer-knowledge"] + [
                    f"/v1/internal/consumer-data/{kind}"
                    for kind in ("status", "billing", "knowledge")
                    if kind != capability
                ]
                for path in paths:
                    r = env["client"].post(path, headers=headers)
                    assert r.status_code == 403, r.text
                    assert r.json()["error"]["code"] == "GRANT_CAPABILITY_MISMATCH"
                assert not kb.calls and env["upstream"].calls == 0
                path = "/v1/internal/consumer-data/" + capability
                r = env["client"].post(path, headers=headers)
                assert r.status_code == 200, r.text
                assert env["client"].post(path, headers=headers).json() == r.json()
                for wrong in paths:
                    assert env["client"].post(wrong, headers=headers).status_code == 403
                return ChatflowReply(
                    *ids,
                    json.dumps(
                        {
                            "kind": "realtime" if capability == "status" else "knowledge",
                            "faq_id": "" if capability == "status" else "KB-C-007",
                        }
                    ),
                    {},
                )

        monkeypatch.setattr("aiops_diagnostics.consumer_chat_service.DifyChatflowClient", Flow)
        response = ask(env, question)
        assert response.status_code == 202, response.text
        result = done(env, response.json()["qa_id"]).json()
        assert result["status"] == "completed", result
        assert env["upstream"].calls == (1 if capability == "status" else 0)
        assert len(kb.calls) == (0 if capability == "status" else 1)


def test_legacy_binding_keeps_its_fingerprint_and_old_callback(tmp_path, monkeypatch):
    import hashlib
    from dataclasses import asdict

    from test_app_runtime_node_entry import _Resolver

    from aiops_diagnostics.chat_execution import select_binding
    from aiops_diagnostics.consumer_knowledge import _binding_identity, digest

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        document = json.loads(env["bindings"].read_text())
        document["bindings"][0].pop("callback_version")
        env["bindings"].write_text(json.dumps(document))
        caller = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
        binding = select_binding(env["settings"], caller, scenario="consumer_service")
        fields = asdict(binding)
        fields.pop("callback_version")
        expected = hashlib.sha256(
            (json.dumps(fields, sort_keys=True) + digest(binding.content_file)).encode()
        ).hexdigest()
        assert _binding_identity(binding) == expected
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        assert env["upstream"].calls == 1 and not kb.calls
