"""Source-contract guards for the T6 Java `/diag/device` deliverable.

The production Java repository is remote and this checkout has no JDK, so the
runner cannot compile or boot the Spring service. These tests pin the observable
contract of the checked-in Java source artifact that lands in the team
repository: routing, header self-validation, response envelope, query bounds,
device lookup fields and audit-safety properties. They are static contract
guards, not a substitute for deploying the service and running the Gherkin
acceptance scenarios in `docs/diag-query-api-plan.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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

DEVICE_COLUMNS = [
    "id",
    "tenant_id",
    "site_id",
    "device_code",
    "protocol",
    "online_status",
    "status",
    "work_status",
    "error_reason",
    "fee_template_id",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _device_columns(sql_name: str = "DEVICE_BY_ID_SQL") -> list[str]:
    source = _read(CONTROLLER)
    match = re.search(
        rf'String {re.escape(sql_name)} = """\s*SELECT (.*?)\s*FROM iot_charging_device',
        source,
        re.DOTALL,
    )
    assert match is not None
    return [column for column in re.split(r"[\s,]+", match.group(1).replace("\n", " ")) if column]


def _sql_block(sql_name: str) -> str:
    source = _read(CONTROLLER)
    match = re.search(rf'String {re.escape(sql_name)} = """(.*?)""";', source, re.DOTALL)
    assert match is not None
    return match.group(1)


def test_controller_routes_get_device_under_diag_root() -> None:
    source = _read(CONTROLLER)
    assert '@RestController' in source
    assert '@RequestMapping("/diag")' in source
    assert '@GetMapping("/device")' in source


def test_controller_self_validates_internal_token_headers() -> None:
    source = _read(CONTROLLER)
    assert '@RequestHeader(value = "X-Internal-Token", required = false)' in source
    assert '@RequestHeader(value = "X-Request-Timestamp", required = false)' in source
    assert 'internalTokenManager.validateToken(' in source


def test_invalid_token_returns_http_401_and_r_envelope() -> None:
    source = _read(CONTROLLER)
    assert 'HttpStatus.UNAUTHORIZED' in source
    assert 'R.failed(401, "令牌无效或过期")' in source


def test_device_payload_uses_r_envelope_and_pinned_fields() -> None:
    source = _read(CONTROLLER)
    assert 'ResponseEntity<R<Map<String, Object>>>' in source
    assert _device_columns() == DEVICE_COLUMNS


def test_device_lookup_requires_exactly_one_of_device_id_or_device_code() -> None:
    source = _read(CONTROLLER)
    assert '@RequestParam(value = "device_id", required = false)' in source
    assert '@RequestParam(value = "device_code", required = false)' in source
    assert 'boolean hasDeviceId = normalizedDeviceId != null;' in source
    assert 'boolean hasDeviceCode = normalizedDeviceCode != null;' in source
    assert 'if (hasDeviceId == hasDeviceCode)' in source
    assert 'R.failed(400, "非法参数")' in source


def test_device_query_is_parameterized_read_only_and_capped_at_one() -> None:
    source = _read(CONTROLLER)
    assert '@Transactional(readOnly = true' in source
    assert 'FROM iot_charging_device' in source
    assert 'WHERE id = ?' in source
    assert 'WHERE device_code = ?' in source
    assert 'AND (? IS NULL OR tenant_id = ?)' in source
    assert 'LIMIT 1' in source
    assert 'jdbcTemplate.queryForList' in source


def test_device_parameter_rejects_injection_characters_before_query() -> None:
    source = _read(CONTROLLER)
    assert '^[A-Za-z0-9_.:-]{1,128}$' in source
    assert 'isSafeValue(deviceValue)' in source
    assert source.index('isSafeValue(deviceValue)') < source.index(
        'jdbcTemplate.queryForList(deviceSql'
    )


def test_blank_tenant_id_is_normalized_to_cross_tenant_null() -> None:
    source = _read(CONTROLLER)
    assert 'String queryTenantId = blankToNull(tenantId);' in source
    assert 'queryForList(deviceSql, deviceValue, queryTenantId, queryTenantId)' in source


def test_tenant_id_is_optional_and_rejected_before_query_if_unsafe() -> None:
    source = _read(CONTROLLER)
    assert '@RequestParam(value = "tenant_id", required = false)' in source
    assert '(queryTenantId != null && !isSafeValue(queryTenantId))' in source
    assert source.index('String queryTenantId = blankToNull(tenantId);') < source.index(
        'isSafeValue(deviceValue)'
    )
    assert source.index('isSafeValue(queryTenantId)') < source.index(
        'jdbcTemplate.queryForList(deviceSql'
    )


def test_device_sql_variants_pin_same_columns_and_only_filter_by_unique_lookup() -> None:
    id_sql = _sql_block("DEVICE_BY_ID_SQL")
    code_sql = _sql_block("DEVICE_BY_CODE_SQL")
    assert _device_columns("DEVICE_BY_ID_SQL") == DEVICE_COLUMNS
    assert _device_columns("DEVICE_BY_CODE_SQL") == DEVICE_COLUMNS
    assert 'WHERE id = ?' in id_sql
    assert 'WHERE device_code = ?' in code_sql
    assert id_sql.replace("WHERE id = ?", "WHERE device_code = ?") == code_sql


def test_each_device_lookup_binds_three_placeholders() -> None:
    for sql_name in ("DEVICE_BY_ID_SQL", "DEVICE_BY_CODE_SQL"):
        assert _sql_block(sql_name).count("?") == 3


def test_device_lookup_reuses_selected_sql_after_key_normalization() -> None:
    source = _read(CONTROLLER)
    assert 'String deviceValue = hasDeviceId ? normalizedDeviceId : normalizedDeviceCode;' in source
    assert 'String deviceSql = hasDeviceId ? DEVICE_BY_ID_SQL : DEVICE_BY_CODE_SQL;' in source
    assert source.index('String deviceSql = hasDeviceId') < source.index(
        'jdbcTemplate.queryForList(deviceSql'
    )


def test_device_miss_returns_null_data() -> None:
    source = _read(CONTROLLER)
    assert 'devices.isEmpty() ? null : devices.get(0)' in source


def test_blank_device_keys_are_normalized_before_exactly_one_guard() -> None:
    source = _read(CONTROLLER)
    guard = "if (hasDeviceId == hasDeviceCode)"
    assert source.index("String normalizedDeviceId = blankToNull(deviceId);") < source.index(
        "boolean hasDeviceId"
    )
    assert source.index("String normalizedDeviceCode = blankToNull(deviceCode);") < source.index(
        "boolean hasDeviceCode"
    )
    assert source.index("boolean hasDeviceCode = normalizedDeviceCode != null;") < source.index(
        guard
    )


def test_audit_aspect_counts_single_device_payload_as_one_row() -> None:
    source = _read(ASPECT)
    assert 'body.path("orders")' in source
    assert 'body.isObject()' in source
    assert 'return body.size() > 0 ? 1 : 0' in source
    assert 'return body.isArray() ? body.size() : 0' not in source


def test_audit_aspect_keeps_existing_order_and_array_row_counts() -> None:
    source = _read(ASPECT)
    assert 'JsonNode orders = body.path("orders");' in source
    assert 'if (orders.isArray())' in source
    assert 'if (body.isArray())' in source
    assert 'return orders.size()' in source
    assert 'return body.size()' in source
