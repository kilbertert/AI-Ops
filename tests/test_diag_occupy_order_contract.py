"""Source-contract guards for the T4 Java `/diag/occupy-order` deliverable.

The production Java repository is remote and this checkout has no JDK, so the
runner cannot compile or boot the Spring service. These tests pin the
observable contract of the checked-in Java source artifact that lands in the
team repository: routing, self-validation, lookup-key semantics, response
envelope, query bounds and the full `ch_occupy_order_info` column list. They
are static contract guards, not a substitute for the Gherkin acceptance
scenarios in `docs/diag-query-api-plan.md`.
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
LANDING_README = PROJECT_ROOT / "java" / "README.md"

OCCUPY_ORDER_COLUMNS = [
    "id",
    "orderId",
    "order_no",
    "device_id",
    "device_code",
    "child_device_id",
    "child_device_code",
    "site_id",
    "userId",
    "free_time",
    "timeout",
    "occupy_amount",
    "pay_amount",
    "status",
    "out_trade_no",
    "is_pay",
    "is_sync_mall_order",
    "pay_time",
    "tenant_id",
    "startTime",
    "endTime",
    "operator_id",
    "refund_status",
    "refund_amount",
    "refund_time",
    "refundRemark",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _columns(source: str, block_name: str) -> list[str]:
    match = re.search(
        rf'String {block_name} = """\s*SELECT (.*?)\s*FROM ch_occupy_order_info',
        source,
        re.DOTALL,
    )
    assert match is not None, f"missing {block_name}"
    return [column for column in re.split(r"[\s,]+", match.group(1)) if column]


def test_t4_java_deliverable_exists() -> None:
    assert CONTROLLER.is_file()
    assert ASPECT.is_file()
    assert LANDING_README.is_file()


def test_controller_routes_get_occupy_order_under_diag_root() -> None:
    source = _read(CONTROLLER)
    assert '@RestController' in source
    assert '@RequestMapping("/diag")' in source
    assert '@GetMapping("/occupy-order")' in source


def test_occupy_order_self_validates_internal_token_headers() -> None:
    source = _read(CONTROLLER)
    assert '@RequestHeader(value = "X-Internal-Token", required = false)' in source
    assert '@RequestHeader(value = "X-Request-Timestamp", required = false)' in source
    assert 'internalTokenManager.validateToken(' in source


def test_invalid_token_returns_http_401_and_r_envelope() -> None:
    source = _read(CONTROLLER)
    assert 'HttpStatus.UNAUTHORIZED' in source
    assert 'R.failed(401, "令牌无效或过期")' in source


def test_occupy_order_returns_r_envelope_with_list_data() -> None:
    source = _read(CONTROLLER)
    assert 'public ResponseEntity<R<List<Map<String, Object>>>> occupyOrder(' in source
    assert 'ch_occupy_order_info' in source
    assert 'R.ok(' in source


def test_occupy_order_requires_exactly_one_lookup_key() -> None:
    source = _read(CONTROLLER)
    assert 'hasExactlyOneLookupKey(' in source
    assert 'HttpStatus.BAD_REQUEST' in source
    assert 'R.failed(400, "非法参数")' in source


def test_occupy_order_maps_order_no_and_order_id_to_the_right_columns() -> None:
    source = _read(CONTROLLER)
    assert 'WHERE order_no = ?' in source
    assert 'WHERE orderId = ?' in source
    assert 'OCCUPY_ORDER_BY_ORDER_NO_SQL' in source
    assert 'OCCUPY_ORDER_BY_ORDER_ID_SQL' in source


def test_occupy_order_selects_full_reference_columns() -> None:
    source = _read(CONTROLLER)
    assert _columns(source, "OCCUPY_ORDER_BY_ORDER_NO_SQL") == OCCUPY_ORDER_COLUMNS
    assert _columns(source, "OCCUPY_ORDER_BY_ORDER_ID_SQL") == OCCUPY_ORDER_COLUMNS


def test_occupy_order_is_parameterized_ordered_and_capped_at_twenty() -> None:
    source = _read(CONTROLLER)
    assert 'ORDER BY startTime DESC' in source
    assert 'LIMIT 20' in source
    assert '@Transactional(readOnly = true' in source
    assert 'AND (? IS NULL OR tenant_id = ?)' in source
    assert 'AND (? IS NULL OR status = ?)' in source


def test_occupy_order_rejects_injection_characters_before_query() -> None:
    source = _read(CONTROLLER)
    assert '^[A-Za-z0-9_.:-]{1,128}$' in source
    assert 'isSafeValue' in source
    assert 'HttpStatus.BAD_REQUEST' in source
    assert 'R.failed(400, "非法参数")' in source


def test_occupy_order_normalizes_blank_tenant_and_status_to_null() -> None:
    source = _read(CONTROLLER)
    assert 'blankToNull' in source
    assert 'queryTenantId' in source
    assert 'queryStatus' in source


def test_java_artifact_does_not_hardcode_internal_token_secret() -> None:
    source = _read(CONTROLLER)
    assert 'qushiyun-internal-secret-2024' not in source
    assert 'InternalTokenManager' in source
