"""节点选库必须决定真实搜索，目录权限不能用于读正文。"""

import json
import uuid

import pytest
from test_app_runtime_dify_entry import dify_chat
from test_consumer_chat_service import Client, done
from test_consumer_flow_contract import BASE, CEILING, graph
from test_consumer_knowledge_flow import ask, setup

from aiops_diagnostics.consumer_flow_contract import ConsumerFlowError, publication_settings
from aiops_diagnostics.consumer_knowledge_selection import PROVIDER, catalogue_token, catalogue_workspace
from aiops_diagnostics.dify_chatflow import ChatflowReply


def picker_graph(ids):
    flow = graph()
    for n in flow["graph"]["nodes"]:
        if n["id"] == "knowledge":
            n["data"] = {
                "type": "tool",
                "provider_type": "builtin",
                "provider_id": PROVIDER,
                "provider_name": PROVIDER,
                "tool_name": "consumer_knowledge",
                "tool_node_version": "2",
                "tool_configurations": {"knowledge_base_ids": {"type": "constant", "value": ids}},
                "tool_parameters": {
                    "data_grant": {"type": "mixed", "value": "{{#start.data_grant#}}"},
                    "image_text": {"type": "mixed", "value": "{{#photo_context.output#}}"},
                },
            }
        if n["id"] == "data_context":
            n["data"]["variables"][0] = ["knowledge", "text"]
    return flow


def test_node_selection_replaces_file_ids_and_requires_current_tenant_ownership():
    flow = picker_graph(["kb-b"])
    result = publication_settings(
        flow,
        {**CEILING, "knowledge_base_ids": ["kb-a"]},
        BASE,
        owned_libraries=[{"knowledge_base_id": "kb-b"}],
    )
    assert result["knowledge_base_ids"] == ["kb-b"] and result["callback_version"] == 3
    for rows in ([], [{"knowledge_base_id": "kb-a"}]):
        with pytest.raises(ConsumerFlowError):
            publication_settings(flow, CEILING, BASE, owned_libraries=rows)
    with pytest.raises(ConsumerFlowError):
        publication_settings(picker_graph([]), CEILING, BASE, owned_libraries=[])
    with pytest.raises(ConsumerFlowError):
        publication_settings(graph(), CEILING, BASE, require_picker=True)


def test_catalogue_credential_is_workspace_and_environment_bound():
    token = catalogue_token("synthetic", "env-a", "workspace-a")
    assert catalogue_workspace(token, "synthetic", "env-a") == "workspace-a"
    for secret, env in [("wrong", "env-a"), ("synthetic", "env-b")]:
        with pytest.raises(ValueError):
            catalogue_workspace(token, secret, env)
    with pytest.raises(ValueError):
        catalogue_workspace(token + "0", "synthetic", "env-a")
    for bad in ["", "ckc_.bad", "ckc_%%%.bad", "ckc_a.b.c"]:
        with pytest.raises(ValueError):
            catalogue_workspace(bad, "synthetic", "env-a")
    with pytest.raises(ValueError):
        catalogue_token("", "env-a", "workspace-a")


def test_catalogue_outage_and_malformed_node_fail_closed():
    from types import SimpleNamespace

    from aiops_diagnostics.consumer_knowledge_selection import owned_libraries, selected_libraries

    with pytest.raises(ValueError, match="目录暂不可用"):
        owned_libraries(SimpleNamespace(kb_search_client=None), "tenant")
    with pytest.raises(ValueError, match="配置常量"):
        selected_libraries({"tool_configurations": {"knowledge_base_ids": ["kb-a"]}})


@pytest.mark.parametrize("selection", [None, "kb-a", [], ["kb-a", "kb-a"], [1], ["../other"], ["a"] * 21])
def test_malformed_selection_is_refused(selection):
    with pytest.raises(ConsumerFlowError):
        publication_settings(picker_graph(selection), CEILING, BASE, owned_libraries=[])


@pytest.mark.parametrize("phase", ["search", "finish", "readback"])
def test_removing_library_refuses_delivery_at_each_boundary(tmp_path, monkeypatch, phase):
    from test_app_runtime_dify_entry import HEADERS

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0]["callback_version"] = 3
        env["bindings"].write_text(json.dumps(doc))
        libraries = [{"knowledge_base_id": "kb-own"}]
        monkeypatch.setattr(kb, "list_knowledge_bases", lambda: libraries, raising=False)
        if phase == "search":
            kb.after = libraries.clear
        responses = []

        class Flow(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                response = env["client"].post(
                    "/v1/internal/consumer-data/knowledge",
                    headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                    json={"knowledge_base_ids": ["kb-own"]},
                )
                responses.append(response)
                if phase == "finish":
                    libraries.clear()
                return ChatflowReply(*ids, json.dumps({"kind": "knowledge", "faq_id": "KB-C-007"}), {})

        monkeypatch.setattr("aiops_diagnostics.consumer_chat_service.DifyChatflowClient", Flow)
        qa = ask(env).json()
        result = done(env, qa["qa_id"]).json()
        assert responses[0].status_code == (503 if phase == "search" else 200)
        if phase != "readback":
            assert result["status"] == "failed" and result["result"] is None
            run = runtime.dify_run_store.by_qa(qa["qa_id"])
            assert run.reason == "KNOWLEDGE_SELECTION_UNAVAILABLE"
            if phase == "search":
                run = runtime.dify_run_store.by_qa(qa["qa_id"])
                assert runtime.dify_run_store.context(run.run_id) is None
        else:
            assert result["status"] == "completed"
            libraries.clear()
            response = env["client"].get("/v1/assistant/questions/" + qa["qa_id"], headers=HEADERS)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "KNOWLEDGE_SELECTION_UNAVAILABLE"
            assert "按设备提示启动" not in response.text


def test_picker_calls_only_selected_library_and_rejects_mismatch_before_cache(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0].update(callback_version=3, knowledge_base_ids=["kb-b"])
        env["bindings"].write_text(json.dumps(doc))
        monkeypatch.setattr(
            kb, "list_knowledge_bases", lambda: [{"knowledge_base_id": "kb-b", "name": "B"}], raising=False
        )
        original = kb.search

        def search(ids, question, top_k):
            assert ids == ("kb-b",)
            rows = original(ids, question, top_k)
            return [{**row, "knowledge_base_id": "kb-b"} for row in rows]

        monkeypatch.setattr(kb, "search", search)

        class Flow(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                headers = {"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]}
                path = "/v1/internal/consumer-data/knowledge"
                for selected in (None, ["kb-a"], ["foreign"]):
                    r = env["client"].post(path, headers=headers, json={"knowledge_base_ids": selected})
                    assert r.status_code == 403, r.text
                assert not kb.calls
                good = env["client"].post(path, headers=headers, json={"knowledge_base_ids": ["kb-b"]})
                assert good.status_code == 200, good.text
                assert (
                    env["client"]
                    .post(path, headers=headers, json={"knowledge_base_ids": ["kb-a"]})
                    .status_code
                    == 403
                )
                monkeypatch.setattr(kb, "list_knowledge_bases", lambda: [])
                assert (
                    env["client"]
                    .post(path, headers=headers, json={"knowledge_base_ids": ["kb-b"]})
                    .status_code
                    == 503
                )
                monkeypatch.setattr(kb, "list_knowledge_bases", lambda: [{"knowledge_base_id": "kb-b"}])
                return ChatflowReply(*ids, json.dumps({"kind": "knowledge", "faq_id": "KB-C-007"}), {})

        errors = []
        original_run = Flow.run

        def invoke(self, **kwargs):
            try:
                return original_run(self, **kwargs)
            except Exception as exc:
                errors.append(str(exc))
                raise

        monkeypatch.setattr(Flow, "run", invoke)
        monkeypatch.setattr("aiops_diagnostics.consumer_chat_service.DifyChatflowClient", Flow)
        qa = ask(env).json()
        result = done(env, qa["qa_id"]).json()
        assert not errors, errors
        assert result["status"] == "completed", result
        assert len(kb.calls) == 1 and env["upstream"].calls == 0


def test_catalogue_endpoint_only_lists_its_credential_tenant(tmp_path, monkeypatch):
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        env["settings"].internal_token_secret = "catalogue-test"
        env["settings"].environment = "test"
        runtime.dify_workspaces = DifyWorkspaces(env["db"])
        env["app"].state.gateway.dify_workspaces = runtime.dify_workspaces
        runtime.dify_workspaces.claim("TENANT-A", "test", owner="test", lease_seconds=60)
        runtime.dify_workspaces.mark_created("TENANT-A", "test", "workspace-a")
        monkeypatch.setattr(
            kb,
            "list_knowledge_bases",
            lambda: [{"knowledge_base_id": "kb-own", "name": "已审核FAQ"}],
            raising=False,
        )
        token = catalogue_token("catalogue-test", "test", "workspace-a")
        path = "/v1/internal/consumer-knowledge/catalogue"
        r = env["client"].get(path, headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200 and r.json()["libraries"] == [
            {"knowledge_base_id": "kb-own", "name": "已审核FAQ"}
        ]
        assert not kb.calls
        assert (
            env["client"].get(path, headers={"Authorization": "Bearer " + token + "bad"}).status_code == 403
        )
        assert (
            env["client"]
            .post("/v1/internal/consumer-data/knowledge", headers={"Authorization": "Bearer " + token})
            .status_code
            == 401
        )


def test_picker_upgrade_preserves_other_nodes_and_library_selection():
    import copy
    import importlib.util
    from pathlib import Path

    import yaml

    spec = importlib.util.spec_from_file_location(
        "upgrade_picker", Path(__file__).parents[1] / "tools/upgrade_consumer_flow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = yaml.safe_load(
        (Path(__file__).parents[1] / "deploy/dify-chat/consumer-service.dsl.yml").read_text()
    )
    before = copy.deepcopy(source)
    changed = module.select_knowledge(source, ["kb-b"], "trendpower/consumer_knowledge:0.0.1@abc")
    assert source == before
    for original, actual in zip(
        source["workflow"]["graph"]["nodes"], changed["workflow"]["graph"]["nodes"], strict=True
    ):
        if original["id"] not in {"knowledge", "data_context"}:
            assert original == actual
    flow = changed["workflow"]
    for n in flow["graph"]["nodes"]:
        if "url" in n["data"]:
            n["data"]["url"] = n["data"]["url"].replace("http://gateway.invalid", BASE)
    assert publication_settings(flow, CEILING, BASE, owned_libraries=[{"knowledge_base_id": "kb-b"}])[
        "knowledge_base_ids"
    ] == ["kb-b"]


def test_plugin_never_redirects_credentials_and_uses_grant_for_content(monkeypatch):
    import importlib.util
    import urllib.error
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "knowledge_plugin_client", Path(__file__).parents[1] / "plugins/consumer_knowledge/client.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

    def opener(handler):
        assert handler.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.test") is None
        return Opener()

    monkeypatch.setattr(module.urllib.request, "build_opener", opener)
    credentials = {"gateway_url": "https://gateway.test", "catalogue_token": "catalogue-test"}
    with pytest.raises(ValueError, match="知识目录或检索暂不可用"):
        module.call(credentials, "/v1/internal/consumer-data/knowledge", grant="round-test", body={})
    assert len(requests) == 1

    assert requests[0].get_header("Authorization") == "Bearer round-test"
    assert "catalogue-test" not in str(requests[0].headers)
    for url in [
        "https://",
        "http://example.test",
        "https://user:pass@gateway.test",
        "https://gateway.test/path",
    ]:
        with pytest.raises(ValueError):
            module.call({**credentials, "gateway_url": url}, "/v1/internal/consumer-knowledge/catalogue")
    assert len(requests) == 1


def test_unavailable_previous_knowledge_does_not_block_new_order(tmp_path, monkeypatch):
    from test_app_runtime_dify_entry import HEADERS
    from test_consumer_live_context import ask as question
    from test_consumer_live_context import wire

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0]["callback_version"] = 3
        env["bindings"].write_text(json.dumps(doc))
        libraries = [{"knowledge_base_id": "kb-own"}]
        monkeypatch.setattr(kb, "list_knowledge_bases", lambda: libraries, raising=False)

        class Flow(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                kind = kwargs["inputs"]["data_kind"]
                response = env["client"].post(
                    "/v1/internal/consumer-data/" + kind,
                    headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                    **({"json": {"knowledge_base_ids": ["kb-own"]}} if kind == "knowledge" else {}),
                )
                assert response.status_code == 200, response.text
                if kind == "status":
                    assert kwargs["conversation_id"] == ""
                return ChatflowReply(
                    *ids,
                    json.dumps(
                        {
                            "kind": "knowledge" if kind == "knowledge" else "realtime",
                            "faq_id": "KB-C-007" if kind == "knowledge" else "",
                        }
                    ),
                    {},
                )

        monkeypatch.setattr("aiops_diagnostics.consumer_chat_service.DifyChatflowClient", Flow)
        intro = question(env, "怎么开始充电？").json()
        assert done(env, intro["qa_id"]).json()["status"] == "completed"
        libraries.clear()
        assert (
            env["client"].get("/v1/assistant/questions/" + intro["qa_id"], headers=HEADERS).status_code == 503
        )
        response = question(env, "check my latest order status", intro["conversation_id"])
        assert response.status_code == 202, response.text
        assert done(env, response.json()["qa_id"]).json()["status"] == "completed"
        assert env["upstream"].calls == 1 and len(kb.calls) == 1
