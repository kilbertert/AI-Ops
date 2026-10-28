"""公开规范是现有接口的投影，不是另一套业务路由或认证器。"""

import json
import re
from pathlib import Path

import pytest
from fastapi.openapi.models import OpenAPI
from test_faq_gateway_api import _client

from aiops_diagnostics.gateway_api import AssistantQuestionRequest
from aiops_diagnostics.knowledge_retrieval import ALLOWED_MEDIA_TYPES, MediaResourceSigner


def test_public_operations_match_testing_guide_and_exclude_management(tmp_path):
    with _client(tmp_path) as client:
        # 同一路由多方法也不能顺带公开未选择的方法。
        client.app.add_api_route("/v1/shortcuts", lambda: {}, methods=["GET", "PATCH"])
        response = client.get("/v1/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    OpenAPI.model_validate(spec)
    guide = (Path(__file__).parents[1] / "docs/testing-api-guide.md").read_text()
    documented = {
        (m.lower(), p.split("?")[0])
        for m, p in re.findall(r"\| [A-Z]\d{2} \| `(GET|POST|DELETE) ([^`]+)`", guide)
    }
    actual = {(method, path) for path, item in spec["paths"].items() for method in item}
    assert len(actual) == 23 and actual == documented
    serialized = json.dumps(spec)
    for private in (
        "/internal/",
        "/v1/agents",
        "X-AIOps-Source-Key",
        "x-third-session",
        "server_config",
        "DifyPublishRequest",
        "Classifier routes",
    ):
        assert private not in serialized
    assert "post" not in spec["paths"]["/v1/shortcuts"]
    # 验证每一个JSON指针，防止删除默认422模型后留下悬空引用。
    for reference in re.findall(r'"\$ref": "([^"]+)"', serialized):
        assert reference.startswith("#/")
        target = spec
        for part in reference[2:].split("/"):
            target = target[part]


def test_docs_are_public_but_business_still_requires_identity(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/v1/docs", headers={"Host": "untrusted.invalid"})
        spec = client.get("/v1/openapi.json", headers={"Host": "untrusted.invalid"})
        assert client.get("/v1/faq/catalog").status_code == 401
        assert client.post("/v1/assistant/questions", json={"question": "你好"}).status_code == 401
        assert client.get("/openapi.json").status_code == 404
        assert client.post("/v1/openapi.json").status_code == 405
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'href="/v1/openapi.json"' in page.text and "Apifox" in page.text
    assert "<script" not in page.text and "untrusted.invalid" not in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert spec.json()["servers"][0]["url"] == "https://api.mall.qushiyun.com"


def test_external_auth_media_and_analysis_are_described_separately(tmp_path):
    with _client(tmp_path) as client:
        spec = client.get("/v1/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["ConsumerSession"]["name"] == "third-session"
    assert schemes["OperatorBearer"]["scheme"] == "bearer"
    for path, item in spec["paths"].items():
        for operation in item.values():
            if path == "/v1/media/{signed_id}":
                assert operation["security"] == []
                assert [p["name"] for p in operation["parameters"]] == ["signed_id", "Range"]
                assert "206" in operation["responses"]
            elif path.startswith("/v1/standard/analyses"):
                assert operation["security"] == [{"OperatorBearer": []}]
            else:
                assert operation["security"] == [{"ConsumerSession": []}, {"OperatorBearer": []}]
            assert operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
                "/ErrorEnvelope"
            )


def test_request_constraints_examples_and_async_ids_follow_implementation(tmp_path):
    with _client(tmp_path) as client:
        spec = client.get("/v1/openapi.json").json()
    schema = spec["components"]["schemas"]
    expected = AssistantQuestionRequest.model_json_schema()
    # FastAPI省略默认值为null的属性，不改变字段约束。
    for prop in expected["properties"].values():
        if prop.get("default") is None:
            prop.pop("default", None)
    assert schema["AssistantQuestionRequest"]["properties"] == expected["properties"]
    assert schema["AssistantQuestionRequest"]["required"] == ["question"]
    assert schema["AssistantQuestionRequest"]["additionalProperties"] is False
    operation = spec["paths"]["/v1/assistant/questions"]["post"]
    for example in operation["requestBody"]["content"]["application/json"]["examples"].values():
        AssistantQuestionRequest.model_validate(example["value"])
    variants = operation["responses"]["202"]["content"]["application/json"]["schema"]["anyOf"]
    assert {v["$ref"].split("/")[-1] for v in variants} == {"QA", "Diagnosis", "Analysis"}
    for name, identifier in [("QA", "qa_id"), ("Diagnosis", "diagnosis_id"), ("Analysis", "analysis_id")]:
        assert identifier in schema[name]["required"]
    for path in ("/v1/assistant/questions/{qa_id}/cancel", "/v1/standard/diagnoses/{diagnosis_id}/cancel"):
        assert "requestBody" not in spec["paths"][path]["post"]
        assert "200" in spec["paths"][path]["post"]["responses"]


@pytest.mark.parametrize("mime", sorted(ALLOWED_MEDIA_TYPES))
def test_media_response_content_type_and_range_match_schema(tmp_path, mime):
    from test_media_api import _client as media_client
    from test_media_api import _issue, _Runtime

    signer = MediaResourceSigner("isolated-test-secret", ttl_seconds=600)
    signed_id = _issue(signer, kind=mime.split("/")[0], mime=mime)
    with media_client(tmp_path, _Runtime(signer=signer, fetch=lambda _: b"synthetic-media")) as client:
        operation = client.get("/v1/openapi.json").json()["paths"]["/v1/media/{signed_id}"]["get"]
        invalid = client.get("/v1/media/invalid-signature")
        assert invalid.status_code == 403 and invalid.content == b""
        assert "content" not in operation["responses"]["403"]
        for headers, code in [({}, 200), ({"Range": "bytes=0-2"}, 206), ({"Range": "bytes=100-200"}, 416)]:
            response = client.get(f"/v1/media/{signed_id}", headers=headers)
            assert response.status_code == code
            documented = operation["responses"][str(code)]
            if code == 416:
                assert response.content == b"" and "content" not in documented
            else:
                assert response.headers["content-type"] == mime
                assert mime in documented["content"]
