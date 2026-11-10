"""订单通讯与事件的有界候选元数据；不交付未知报文正文。"""

import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from threading import BoundedSemaphore
from time import monotonic
from zoneinfo import ZoneInfo

from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc, parse_stored_time
from aiops_diagnostics.operator.device_offline import instant
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

NAME = "cpms_order_communications"
DETAIL = "cpms_order_detail"
SOURCE_LIMIT = 12
MAX_WINDOW = timedelta(hours=24)
# 通讯正文/方向/代码的生产列合同未证实，只查询写入路径已确认的时间与设备标签。
SOURCES = (
    ("charging-pile_comm", "deviceCode", None),
    ("charging-pile_event", "deviceCode", "event"),
    ("charging-gun_event", "childDeviceCode", "event"),
    ("charging-pile_fi", "deviceCode", "functionId"),
    ("charging-gun_fi", "childDeviceCode", "functionId"),
)
LIMIT = SOURCE_LIMIT * len(SOURCES)
SOURCE_BUDGET_SECONDS = 4.0
# 进程级有界；超时底层读取仍占槽位，后续请求不会堆积线程或排队任务。
_SOURCE_POOL = ThreadPoolExecutor(max_workers=len(SOURCES), thread_name_prefix="order-source")
_SOURCE_SLOTS = BoundedSemaphore(len(SOURCES))


def _read_before(read, url, payload, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    result = read(url, payload, timeout=remaining)
    if monotonic() > deadline:
        raise TimeoutError
    return result


def valid_device_code(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def order_window(detail, as_of, timezone):
    """沿用已声明业务时区；不从枪编码猜父设备，不把无效停时当充电中。"""
    device = detail.get("deviceCode")
    if not valid_device_code(device):
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    try:
        zone = ZoneInfo(timezone)
        now = instant(as_of, zone)
        start = instant(detail.get("createdTime"), zone)
        end = instant(detail.get("stopTime"), zone)
        if end is None:
            if type(detail.get("status")) is not int or detail["status"] != 0:
                raise ValueError("missing stop time")
            end = now
        if start is None or now is None or not start <= end <= now:
            raise ValueError("invalid order window")
    except (ValueError, TypeError, KeyError) as exc:
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING") from exc
    capped_end = min(end, start + MAX_WINDOW)
    return {
        "device": device,
        "window_start": format_stored_time(start),
        "window_end": format_stored_time(capped_end),
        "order_window_end": format_stored_time(end),
        "window_truncated": capped_end < end,
        "timezone": timezone,
        "sample_limit": LIMIT,
    }


def _records(data, device, bounds, event_field):
    rows = data.get("records") if isinstance(data, dict) else None
    count = data.get("count") if isinstance(data, dict) else None
    if (
        not isinstance(rows, list)
        or type(count) is not int
        or not 0 <= len(rows) <= SOURCE_LIMIT
        or count < len(rows)
        or (not rows and count != 0)
    ):
        raise ValueError("invalid source page")
    start, end = (parse_stored_time(bounds[k]) for k in ("window_start", "window_end"))
    zone, previous, result = ZoneInfo(bounds["timezone"]), start, []
    for row in rows:
        if not isinstance(row, dict) or row.get("device") != device:
            raise ValueError("source device mismatch")
        time = instant(row.get("_ts"), zone)
        if time is None or not previous <= time <= end:
            raise ValueError("source time mismatch")
        previous = time
        event = row.get(event_field) if event_field else None
        if event is not None and (
            not isinstance(event, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", event)
        ):
            raise ValueError("invalid event name")
        result.append(
            {"ts": format_stored_time(time), "event": event, "association": "device_window_candidate"}
        )
    return result, count


def read_sources(detail, bounds, base, read):
    sources, pending = [], []
    deadline = monotonic() + SOURCE_BUDGET_SECONDS
    zone = ZoneInfo(bounds["timezone"])
    # QueryRequest 使用源库本地时间，与既有 charging_timeline 的门面合同相同。
    start, end = (
        parse_stored_time(bounds[k]).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        for k in ("window_start", "window_end")
    )
    for table, device_key, event_field in SOURCES:
        source = {
            "table": table,
            "status": "unavailable",
            "reason": None,
            "records": [],
            "total": None,
            "truncated": None,
            "sample_limit": SOURCE_LIMIT,
        }
        sources.append(source)
        device = detail.get(device_key)
        if not valid_device_code(device):
            source["reason"] = "missing_device"
            continue
        if not base:
            source["reason"] = "source_not_configured"
            continue
        if not _SOURCE_SLOTS.acquire(blocking=False):
            source["reason"] = "source_busy"
            continue
        try:
            future = _SOURCE_POOL.submit(
                _read_before,
                read,
                base.rstrip("/") + "/tdadmin/data/query",
                {
                    "table": table,
                    "deviceId": device,
                    "start": start,
                    "end": end,
                    "fields": ["_ts", "device", *([event_field] if event_field else [])],
                    "orderBy": "_ts",
                    "order": "ASC",
                    "page": 1,
                    "pageSize": SOURCE_LIMIT,
                },
                deadline,
            )
        except BaseException:
            _SOURCE_SLOTS.release()
            raise
        future.add_done_callback(lambda _: _SOURCE_SLOTS.release())
        pending.append((source, future, device, event_field))
    if pending:
        wait([item[1] for item in pending], timeout=max(0, deadline - monotonic()))
    for source, future, device, event_field in pending:
        if not future.done():
            future.cancel()
            source["reason"] = "source_timeout"
            continue
        try:
            data = future.result()
            rows, count = _records(data, device, bounds, event_field)
        except TimeoutError:
            source["reason"] = "source_timeout"
            continue
        except OperatorRefused as exc:
            if str(exc) != "TSDATA_UNAVAILABLE":
                raise
            source["reason"] = "source_unavailable"
            continue
        except (ValueError, TypeError, KeyError):
            source["reason"] = "invalid_source_response"
            continue
        source.update(
            status="records" if rows else "empty", records=rows, total=count, truncated=count > len(rows)
        )
    return {
        **bounds,
        "sources": sources,
        "association": "device_window_candidate",
        "limitations": (
            "仅设备窗口候选，未关联本单交易；时间为入库时间；未读取报文正文、方向、代码及解码字段。"
        ),
    }


def require_current(owner, task, run):
    owner.authorize_execution(run.run_id, tool=NAME)
    if DETAIL not in task.delivered:
        raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")
    detail = task.evidence.get(DETAIL, {}).get("data") or {}
    if detail.get("orderNo") != run.order_no:
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    current = owner._authorized_order(task, run.order_no, expected_site=task.order_site_id)
    current_detail = current["data"].get("chOrderInfoModel") or current["data"]
    if any(
        current_detail.get(key) != detail.get(key)
        for key in ("deviceCode", "childDeviceCode", "createdTime", "stopTime", "status")
    ):
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    return detail


def query(owner, task, run, read):
    detail = require_current(owner, task, run)
    bounds = order_window(detail, now_utc(), owner.context.runtime.diagnostic_settings.mysql.timezone)

    result = read_sources(detail, bounds, owner.context.settings.operator_tsdata_url, read)
    require_current(owner, task, run)
    return result
