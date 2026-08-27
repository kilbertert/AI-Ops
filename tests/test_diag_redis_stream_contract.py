"""Source-contract guards for the T5 Java `/diag/redis-stream` deliverable.

The production Java repository is remote and this checkout has no JDK, so the
runner cannot compile or boot the Spring service. These tests pin the external
contract of the checked-in Java artifact instead: routing, token rejection,
Stream whitelist enforcement, the server-side `max_messages` cap, the bounded
reverse-range query and the response shape. They are static guards, not a
substitute for deploying the service and running the Gherkin acceptance tests in
`docs/diag-query-api-plan.md`.
"""

from __future__ import annotations

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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_t5_java_deliverable_exists() -> None:
    assert CONTROLLER.is_file()
    assert ASPECT.is_file()
    assert LANDING_README.is_file()


def test_controller_routes_get_redis_stream_under_diag_root() -> None:
    source = _read(CONTROLLER)
    assert "@RestController" in source
    assert '@RequestMapping("/diag")' in source
    assert '@GetMapping("/redis-stream")' in source


def test_controller_self_validates_internal_token_headers() -> None:
    source = _read(CONTROLLER)
    assert '@RequestHeader(value = "X-Internal-Token", required = false)' in source
    assert '@RequestHeader(value = "X-Request-Timestamp", required = false)' in source
    assert "internalTokenManager.validateToken(" in source


def test_invalid_token_returns_http_401_and_r_envelope() -> None:
    source = _read(CONTROLLER)
    assert "HttpStatus.UNAUTHORIZED" in source
    assert 'R.failed(401, "令牌无效或过期")' in source


def test_redis_stream_only_allows_the_two_whitelisted_streams() -> None:
    source = _read(CONTROLLER)
    assert '"third.order.sync.queue"' in source
    assert '"third.order.sync.notify.queue"' in source
    assert "isAllowedStream(" in source


def test_non_whitelisted_stream_is_rejected_before_redis_access() -> None:
    source = _read(CONTROLLER)
    assert "HttpStatus.BAD_REQUEST" in source
    assert 'R.failed(400, "非白名单 Stream")' in source
    assert source.index("isAllowedStream(") < source.index("opsForStream()")


def test_missing_token_is_rejected_before_parameter_or_redis_checks() -> None:
    source = _read(CONTROLLER)
    assert source.index("internalTokenManager.validateToken(") < source.index("isAllowedStream(")


def test_max_messages_has_a_hard_server_side_cap() -> None:
    source = _read(CONTROLLER)
    assert "REDIS_STREAM_MAX_MESSAGES = 1000" in source
    assert '@RequestParam(value = "max_messages", defaultValue = "1000")' in source
    assert "Math.min(" in source


def test_max_messages_rejects_non_positive_values() -> None:
    source = _read(CONTROLLER)
    assert "Math.max(1, Math.min(maxMessages, REDIS_STREAM_MAX_MESSAGES))" in source


def test_redis_stream_uses_bounded_reverse_range_query() -> None:
    source = _read(CONTROLLER)
    assert "StringRedisTemplate" in source
    assert "opsForStream()" in source
    assert "reverseRange(" in source
    assert "Limit.limit().count(" in source
    assert "Range.<String>unbounded()" in source


def test_redis_stream_payload_uses_r_envelope_and_contract_fields() -> None:
    source = _read(CONTROLLER)
    assert "ResponseEntity<R<List<DiagQueryController.DiagRedisStreamResponse>>>" in source
    assert '@JsonProperty("stream")' in source
    assert '@JsonProperty("type")' in source
    assert '@JsonProperty("length")' in source
    assert '@JsonProperty("groups")' in source
    assert '@JsonProperty("inspected_messages")' in source
    assert '@JsonProperty("matches")' in source


def test_redis_stream_group_payload_uses_contract_fields() -> None:
    source = _read(CONTROLLER)
    assert '@JsonProperty("name")' in source
    assert '@JsonProperty("consumers")' in source
    assert '@JsonProperty("pending")' in source
    assert '@JsonProperty("lag")' in source


def test_order_no_matching_is_validated_and_counts_message_fields() -> None:
    source = _read(CONTROLLER)
    assert '@RequestParam(value = "order_no", required = false)' in source
    assert "isSafeValue(orderNo)" in source
    assert "getValue()" in source
    assert "contains(" in source
    assert "messages == null" in source
    assert "if (fields == null)" in source
    assert "field.getKey() != null" in source
    assert "field.getValue() != null" in source


def test_missing_or_non_stream_redis_keys_are_reported_without_empty_groups() -> None:
    source = _read(CONTROLLER)
    assert "DataType.STREAM" in source
    assert "DataType.NONE" in source
    assert "dataType == null" in source
    assert "dataType.code()" in source
    assert "length" in source


def test_stream_lag_tolerates_malformed_last_delivered_ids() -> None:
    source = _read(CONTROLLER)
    assert '"0-0".equals(lastDeliveredId)' in source
    assert "lastDeliveredId.lastIndexOf('-')" in source
    assert "NumberFormatException" in source


def test_audit_aspect_also_covers_redis_stream_endpoint() -> None:
    source = _read(ASPECT)
    assert "DiagQueryController" in source
    assert "@Around" in source
