"""CPMS 充电状态读取；调用方必须先完成本人身份与订单授权。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.data.order_status import KNOWN_STATUS_CODES, StatusRole, role_of
from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url


class ChargingStatusError(RuntimeError):
    pass


def _failure(_failure: HttpFailure) -> ChargingStatusError:
    # 上游异常正文可能包含订单或内部地址，不向调用方转述。
    return ChargingStatusError("charging status upstream unavailable")


def read_charging_status(base_url: str, order_no: str, *, tenant_id: str, user_id: str) -> dict[str, Any]:
    base = canonical_gateway_url(base_url)
    data = request_json(
        RequestSpec(
            url=f"{base}/chOrderInfo/chargingInfo/{quote(order_no, safe='')}",
            headers={"Accept": "application/json", "tenant-id": tenant_id, "user-id": user_id},
            timeout=10,
            max_read_bytes=65536,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=_failure,
            http_error=_failure,
            unavailable=_failure,
            invalid_body=_failure,
            invalid_envelope=_failure,
        ),
        envelope=parse_code_data_envelope,
    )
    if not isinstance(data, dict) or data.get("orderNo") != order_no:
        raise ChargingStatusError("charging status response does not match the authorized order")
    status = data.get("status")
    if isinstance(status, bool) or not isinstance(status, (str, int)) or len(str(status)) > 32:
        raise ChargingStatusError("charging status response has no valid status")
    # 上游字段是整数码或其字符串，不能让兼容转换把 "0.5" 截断为充电中。
    known = str(status) in {str(code) for code in KNOWN_STATUS_CODES}
    return {
        "order_no": order_no,
        "status_code": status,
        "status_role": role_of(status).value if known else StatusRole.UNKNOWN.value,
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "cpms.chargingInfo",
    }
