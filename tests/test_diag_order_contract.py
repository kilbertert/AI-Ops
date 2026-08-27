"""Source-contract guards for the T1 Java `/diag/order` deliverable.

The production Java repository is remote and this checkout has no JDK, so the
runner cannot compile or boot the Spring service. These tests therefore pin the
observable contract of the checked-in Java source artifact that lands in the
team repository: routing, header self-validation, response envelope, query
bounds, fee-template fields and audit behaviour. They are static contract
guards, not a substitute for deploying the service and running the Gherkin
acceptance scenarios in `docs/diag-query-api-plan.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PY_SOURCES = PROJECT_ROOT / "src" / "aiops_diagnostics" / "sources.py"
JAVA_ROOT = (
    PROJECT_ROOT
    / "java"
    / "cloud-charging-pile-web"
    / "src"
    / "main"
    / "java"
    / "com"
    / "qushiyun"
    / "cloud"
    / "charging"
    / "pile"
    / "web"
)
CONTROLLER = JAVA_ROOT / "controller" / "DiagQueryController.java"
ASPECT = JAVA_ROOT / "aspect" / "DiagQueryAuditAspect.java"
LANDING_README = PROJECT_ROOT / "java" / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _order_columns(select: str) -> list[str]:
    return [column for column in re.split(r"[\s,]+", select) if column]


def _python_order_columns() -> list[str]:
    source = _read(PY_SOURCES)
    match = re.search(r'ORDER_COLUMNS = """(.*?)"""', source, re.DOTALL)
    assert match is not None
    return _order_columns(match.group(1).replace("\n", " "))


def _java_order_columns() -> list[str]:
    source = _read(CONTROLLER)
    match = re.search(r'String ORDER_SQL = """\s*SELECT (.*?)\s*FROM ch_order_info', source, re.DOTALL)
    assert match is not None
    return _order_columns(match.group(1).replace("\n", " "))


def test_t1_java_deliverable_exists() -> None:
    assert CONTROLLER.is_file()
    assert ASPECT.is_file()
    assert LANDING_README.is_file()


def test_landing_README_records_local_verification_boundary() -> None:
    source = _read(LANDING_README)
    assert "未编译" in source
    assert "待验证" in source
    assert "DiagQueryController.java" in source
    assert "DiagQueryAuditAspect.java" in source


def test_controller_routes_get_order_under_diag_root() -> None:
    source = _read(CONTROLLER)
    assert "@RestController" in source
    assert '@RequestMapping("/diag")' in source
    assert '@GetMapping("/order")' in source


def test_controller_self_validates_internal_token_headers() -> None:
    source = _read(CONTROLLER)
    assert '@RequestHeader(value = "X-Internal-Token", required = false)' in source
    assert '@RequestHeader(value = "X-Request-Timestamp", required = false)' in source
    assert "internalTokenManager.validateToken(" in source


def test_invalid_token_returns_http_401_and_r_envelope() -> None:
    source = _read(CONTROLLER)
    assert "HttpStatus.UNAUTHORIZED" in source
    assert 'R.failed(401, "令牌无效或过期")' in source


def test_order_payload_uses_r_envelope_and_full_order_fields() -> None:
    source = _read(CONTROLLER)
    assert "ResponseEntity<R<DiagQueryController.DiagOrderResponse>>" in source
    assert "ch_order_info" in source
    for column in (
        "order_no",
        "tenant_id",
        "electricity_fee",
        "service_fee",
        "launch_fee",
        "park_fee",
        "occupy_fee",
        "pay_amount",
        "total_amount",
        "out_trade_no",
        "transaction_id",
        "meter_start",
        "meter_end",
        "start_soc",
        "end_soc",
        "device_protocol",
        "created_time",
        "stop_time",
        "draw_gun_time",
        "tx_data",
    ):
        assert column in source


def test_java_order_select_matches_python_reference_columns() -> None:
    assert _java_order_columns() == _python_order_columns()


def test_order_query_is_parameterized_ordered_and_capped_at_three() -> None:
    source = _read(CONTROLLER)
    assert "ORDER BY created_time DESC" in source
    assert "LIMIT 3" in source
    assert "WHERE order_no = ?" in source
    assert "AND (? IS NULL OR tenant_id = ?)" in source
    assert "@Transactional(readOnly = true" in source


def test_order_parameter_rejects_injection_characters_before_query() -> None:
    source = _read(CONTROLLER)
    assert "^[A-Za-z0-9_.:-]{1,128}$" in source
    assert "isSafeValue(orderNo)" in source
    assert "HttpStatus.BAD_REQUEST" in source
    assert 'R.failed(400, "非法参数")' in source
    assert source.index("isSafeValue(orderNo)") < source.index("queryForList")


def test_blank_tenant_id_is_normalized_to_cross_tenant_null() -> None:
    source = _read(CONTROLLER)
    assert "blankToNull" in source
    assert "queryForList(ORDER_SQL, orderNo, tenantId, tenantId)" not in source
    assert "queryFeeTemplate(orderNo, tenantId)" not in source


def test_fee_template_snapshot_includes_occupy_fee_template() -> None:
    source = _read(CONTROLLER)
    assert "ch_fee_template_record" in source
    assert '@JsonProperty("order_no")' in source
    assert '@JsonProperty("tenant_id")' in source
    assert '@JsonProperty("fee_template")' in source
    assert '@JsonProperty("occupy_fee_template")' in source
    assert '@JsonProperty("period_fee_detail")' in source
    assert "include_fee_template" in source


def test_audit_aspect_records_caller_params_and_result_without_wire_body() -> None:
    source = _read(ASPECT)
    assert "@Aspect" in source
    assert "internal:AIOps" in source
    assert "ProceedingJoinPoint" in source
    assert "elapsedMs" in source
    assert "result" in source
    assert "responseBody" not in source
    assert "queryArgs" in source


def test_audit_aspect_filters_request_headers_by_annotation() -> None:
    source = _read(ASPECT)
    assert "RequestHeader.class" in source
    assert "getParameters()" in source
    assert "isAnnotationPresent" in source


def test_audit_aspect_records_http_error_responses_as_failure() -> None:
    source = _read(ASPECT)
    assert "getStatusCode().isError()" in source
    assert '"failure"' in source
    assert "outcome(result)" in source


def test_java_artifact_does_not_hardcode_internal_token_secret() -> None:
    source = _read(CONTROLLER)
    assert "qushiyun-internal-secret-2024" not in source
    assert "InternalTokenManager" in source
