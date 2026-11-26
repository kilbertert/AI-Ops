"""内置知识库上传代理、默认 FAQ 模板与免 JSON 瞬时三能力自验测试（ADR-0358 P1-4）。

拦截的具体错误：
1. 路径穿越文件名、不支持后缀或超大文件绕过知识库上传校验；
2. 免 JSON 自验时手机号/thirdSession/管家令牌泄露至返回体，或验证异常时忘记吊销临时管家令牌；
3. 订单问句遗漏“过程统计”导致模型跳过过程统计工具。
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.onboarding.wizard_validation import (
    DEFAULT_CONSUMER_STARTER_FAQ_FILENAME,
    MAX_KNOWLEDGE_DOC_BYTES,
    WizardValidationRefused,
    install_consumer_starter_faq,
    run_zero_json_self_validation,
    upload_wizard_knowledge_doc,
)


def test_wizard_knowledge_upload_validates_role_filename_size_and_starter_faq() -> None:
    uploaded: list[tuple[str, str, str, int]] = []

    def fake_uploader(role: str, kb_id: str, filename: str, raw: bytes) -> dict:
        uploaded.append((role, kb_id, filename, len(raw)))
        return {"document_id": "doc_123", "chunk_count": 3}

    res = install_consumer_starter_faq(kb_id="kb_consumer_1", uploader=fake_uploader)
    assert res["target_role"] == "consumer"
    assert res["filename"] == DEFAULT_CONSUMER_STARTER_FAQ_FILENAME
    assert res["starter_template"] is True
    assert res["chunk_count"] == 3

    op_res = upload_wizard_knowledge_doc(
        target_role="operator",
        kb_id="kb_operator_1",
        filename="运维方案.docx",
        content_bytes=b"PK\x03\x04mock",
        uploader=fake_uploader,
    )
    assert op_res["target_role"] == "operator"
    assert op_res["starter_template"] is False

    with pytest.raises(WizardValidationRefused, match="FILENAME_INVALID"):
        upload_wizard_knowledge_doc(
            target_role="consumer",
            kb_id="kb_1",
            filename="../evil.md",
            content_bytes=b"hi",
            uploader=fake_uploader,
        )
    with pytest.raises(WizardValidationRefused, match="EXTENSION_UNSUPPORTED"):
        upload_wizard_knowledge_doc(
            target_role="consumer",
            kb_id="kb_1",
            filename="script.sh",
            content_bytes=b"hi",
            uploader=fake_uploader,
        )
    with pytest.raises(WizardValidationRefused, match="FILE_TOO_LARGE"):
        upload_wizard_knowledge_doc(
            target_role="consumer",
            kb_id="kb_1",
            filename="huge.md",
            content_bytes=b"a" * (MAX_KNOWLEDGE_DOC_BYTES + 1),
            uploader=fake_uploader,
        )


def test_zero_json_self_validation_exchanges_session_revokes_token_and_leaks_zero_secrets() -> None:
    http_log: list[tuple[str, str]] = []

    def fake_http(method: str, url: str, headers: dict[str, str], body):
        http_log.append((method, url))
        if method == "POST" and url.endswith("/mallapi/userinfo/phone/login"):
            return 200, json.dumps({"code": 0, "data": {"thirdSession": "EPHEMERAL_SESS_999"}})
        if method == "DELETE" and url.endswith("/auth/token/logout"):
            assert headers["Authorization"] == "Bearer EPHEMERAL_OP_TOK_777"
            return 200, '{"code":0,"msg":"ok"}'
        raise AssertionError(f"unexpected http {method} {url}")

    seen_cases: dict = {}

    def fake_validator(payload: dict) -> dict:
        seen_cases.update(payload)
        assert payload["consumer_session"] == "EPHEMERAL_SESS_999"
        assert "过程统计" in payload["cases"]["order"]["question"]
        return {
            "capabilities": {
                "consumer": {"state": "passed", "run_id": "tr_1"},
                "order": {"state": "passed", "run_id": "qa_1"},
                "offline": {"state": "passed", "run_id": "qa_2"},
            }
        }

    result = run_zero_json_self_validation(
        environment="jiejia",
        tenant_id="1990990768688599040",
        company_gateway_url="https://api.juxichongdian.cn",
        order_no="2125879212020924417",
        device_id="2121870666501521409",
        offline_start="2026-11-25T00:00:00+08:00",
        offline_end="2026-11-26T23:59:59+08:00",
        consumer_order_no="2092158656121733121",
        consumer_reference_id="10842dd4fa92c98f",
        consumer_phone="13900001111",
        consumer_code="8888",
        operator_token="Bearer EPHEMERAL_OP_TOK_777",
        validator_runner=fake_validator,
        http_caller=fake_http,
    )
    assert result["passed"] is True
    assert result["credentials_persisted"] is False
    assert result["operator_session_revoked"] is True
    assert result["business_accuracy"] == "unconfirmed"
    dumped = json.dumps(result)
    assert "EPHEMERAL_SESS_999" not in dumped
    assert "EPHEMERAL_OP_TOK_777" not in dumped
    assert "13900001111" not in dumped
    assert ("DELETE", "https://api.juxichongdian.cn/auth/token/logout") in http_log

    # 验证异常时 finally 也必须吊销临时管家令牌
    revoked_on_err = {"called": False}

    def err_http(method: str, url: str, headers: dict[str, str], body):
        if method == "DELETE" and url.endswith("/auth/token/logout"):
            revoked_on_err["called"] = True
            return 200, '{"code":0}'
        return 200, json.dumps({"code": 0, "data": {"thirdSession": "S2"}})

    with pytest.raises(RuntimeError, match="boom"):
        run_zero_json_self_validation(
            environment="jiejia",
            tenant_id="1990990768688599040",
            company_gateway_url="https://api.juxichongdian.cn",
            order_no="2125879212020924417",
            device_id="2121870666501521409",
            offline_start="2026-11-25T00:00:00+08:00",
            offline_end="2026-11-26T23:59:59+08:00",
            consumer_order_no="2092158656121733121",
            consumer_reference_id="10842dd4fa92c98f",
            consumer_session="S2",
            operator_token="OP_ERR_TOK",
            validator_runner=lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
            http_caller=err_http,
        )
    assert revoked_on_err["called"] is True
