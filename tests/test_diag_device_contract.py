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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _device_columns() -> list[str]:
    source = _read(CONTROLLER)
    match = re.search(
        r'String DEVICE_BY_ID_SQL = """\s*SELECT (.*?)\s*FROM iot_charging_device',
        source,
        re.DOTALL,
    )
    assert match is not None
    return [column for column in re.split(r"[\s,]+", match.group(1).replace("\n", " ")) if column]


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
    assert _device_columns() == [
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


def test_device_lookup_requires_exactly_one_of_device_id_or_device_code() -> None:
    source = _read(CONTROLLER)
    assert '@RequestParam(value = "device_id", required = false)' in source
    assert '@RequestParam(value = "device_code", required = false)' in source
    assert '(normalizedDeviceId == null) == (normalizedDeviceCode == null)' in source
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
