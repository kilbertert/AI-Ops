"""证据兜底必须保留源值、缺口与显示界限，不能交付未验证正文或凭据。"""

from aiops_diagnostics.operator.operator_evidence import render_evidence


def test_evidence_preserves_values_and_source_gaps_without_inventing_units():
    evidence = {
        "cpms_order_detail": {
            "observed_at": "2026-11-12T00:00:00Z",
            "data": {"orderNo": "2120066268052840449", "totalAmount": "3.9600", "payAmount": 0, "status": 1},
            "response": "PRIVATE-RESPONSE",
        },
        "cpms_order_communications": {
            "observed_at": "2026-11-12T00:00:00Z",
            "data": {
                "sources": [
                    {"table": "charging-pile_comm", "status": "unavailable", "records": []},
                    {"table": "charging-gun_event", "status": "empty", "total": 0, "records": []},
                ]
            },
            "offline_references": "PRIVATE-SCOPE",
        },
    }
    text = render_evidence(evidence, "zh")["blocks"][0]["text"]
    assert "诊断结论未完成" in text and "3.9600" in text and "支付金额记录: 0" in text
    assert "退款金额记录: 未提供" in text and "2120066268052840449" in text
    assert '"unavailable"' in text and '"empty"' in text and "未关联本单交易" in text
    assert "PRIVATE" not in text and "3.9600元" not in text
    assert render_evidence({}, "zh") is None


def test_bounded_excerpts_do_not_escape_the_code_block_or_leak_credentials():
    evidence = {
        "cpms_charging_timeline": {
            "observed_at": "2026-11-12T00:00:00Z",
            "data": {
                "records": [{"status": 3, "errorReason": "```<img src=x> private-bearer"}] * 64,
                "total": 64,
                "truncated": False,
                "complete_timeline": True,
            },
        }
    }
    text = render_evidence(evidence, "en", secrets=("private-bearer",))["blocks"][0]["text"]
    assert "Diagnosis is incomplete" in text and "omitted" in text
    assert "private-bearer" not in text and "<img" not in text
    assert text.count('"status": 3') == 6 and text.count("```") == 2
    assert '"total": 64' in text and '"truncated": false' in text


def test_display_bound_counts_escaped_characters_and_redacts_before_escaping():
    evidence = {
        "cpms_order_detail": {
            "observed_at": "2026-11-12T00:00:00Z",
            "data": {"stoppedReasonContent": "private_token_with_marks"},
        },
        "cpms_charging_samples": {
            "observed_at": "2026-11-12T00:00:00Z",
            "data": {"records": [{"errorReason": "`" * 900}] * 6, "total": 6},
        },
    }
    text = render_evidence(evidence, "zh", secrets=("private_token_with_marks",))["blocks"][0]["text"]
    assert len(text) < 12000 and "部分记录因展示长度限制未列出" in text
    assert "private" not in text and "REDACTED" in text
