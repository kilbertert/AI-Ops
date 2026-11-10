"""设备离线区间与订单记录的确定性关联；不推断物理根因。"""

from collections import Counter, defaultdict
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from aiops_diagnostics.common.scalar_rules import as_datetime, declared_timezone, format_stored_time
from aiops_diagnostics.data.order_status import StatusRole, role_of

SCHEMA = "device-offline/1"
ABNORMAL = {
    StatusRole.UNCONTROLLABLE_EXCEPTION,
    StatusRole.EXCEPTION_ALREADY_HANDLED,
    StatusRole.CONTROLLED_ABNORMAL_END,
}


def instant(value, zone):
    if value is None or value == "":
        return None
    parsed = as_datetime(value)
    if parsed is None:
        raise ValueError("invalid source time")
    if declared_timezone(value) is None:
        parsed = parsed.replace(tzinfo=None)
        # DST重叠或不存在的本地时刻没有唯一解释，不能任选一个offset。
        a, b = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
        if (
            a.utcoffset() != b.utcoffset()
            or a.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed
        ):
            raise ValueError("ambiguous source time")
        parsed = a
    return parsed.astimezone(UTC)


def window(start, end, as_of, timezone):
    zone = ZoneInfo(timezone)
    now = instant(as_of, zone)
    if now is None:
        raise ValueError("query time required")
    if bool(start) != bool(end):
        raise ValueError("请同时提供开始和结束时间")
    if start:
        for value in (start, end):
            if declared_timezone(value) is None:
                raise ValueError("查询时间必须包含时区偏移")
        lo, hi = instant(start, zone), instant(end, zone)
    else:
        lo, hi = now - timedelta(days=7), now
    if not lo < hi <= now or hi - lo > timedelta(days=31):
        raise ValueError("查询窗口须为过去31天以内的递增区间")
    return lo, hi, now, zone


def merged(intervals):
    result = []
    for lo, hi in sorted(intervals):
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(hi, result[-1][1]))
        else:
            result.append((lo, hi))
    return result


def seconds(intervals):
    return int(sum((hi - lo).total_seconds() for lo, hi in merged(intervals)))


def analyze(events, orders, *, start, end, as_of, timezone):
    lo, hi, now, zone = window(start, end, as_of, timezone)
    intervals, by_device, opens = defaultdict(list), {}, defaultdict(list)
    quality = Counter()
    invalid_devices = set()
    unknown_open = Counter()
    raw_count, new_count = Counter(), Counter()
    # 身份由读面校验；事件与订单仍必须是同一租户/设备/站点才能关联。
    for row in events:
        key = (row["tenant_id"], row["device_id"], row["site_id"])
        by_device[key] = {
            k: row.get(k) for k in ("device_id", "device_code", "site_id", "site_name", "online_status")
        }
        try:
            began, ended = instant(row.get("offline_time"), zone), instant(row.get("recover_time"), zone)
            if began is None or began > now or (ended is not None and ended <= began):
                raise ValueError("invalid incident time")
        except (ValueError, TypeError):
            quality["invalid_incident_times"] += 1
            invalid_devices.add(key)
            if row.get("recover_time") is None:
                unknown_open[key] += 1
            continue
        if ended is None or ended > now:
            opens[key].append((began, now))
        if lo <= began < hi:
            new_count[key] += 1
        a, b = max(lo, began), min(hi, ended or now)
        if a < b:
            intervals[key].append((a, b))
            raw_count[key] += 1
    top = [
        {
            **by_device[key],
            "incidents": len(merged(spans)),
            "raw_events": raw_count[key],
            "new_events": new_count[key],
            "offline_seconds": seconds(spans),
        }
        for key, spans in intervals.items()
    ]
    top.sort(key=lambda row: (-row["offline_seconds"], -row["incidents"], row["device_id"]))
    current = [
        {
            **by_device[key],
            "offline_time": format_stored_time(min(a for a, _ in spans)),
            "seconds_open": seconds(spans),
            "unclosed_events": len(spans),
            "platform_state_conflict": by_device[key]["online_status"] == 1,
        }
        for key, spans in opens.items()
    ]
    for key, count in unknown_open.items():
        existing = next(
            (row for row in current if row["device_id"] == key[1] and row["site_id"] == key[2]), None
        )
        if existing is not None:
            existing.update(
                seconds_open=None, offline_time=None, unclosed_events=existing["unclosed_events"] + count
            )
        else:
            current.append(
                {
                    **by_device[key],
                    "offline_time": None,
                    "seconds_open": None,
                    "unclosed_events": count,
                    "platform_state_conflict": by_device[key]["online_status"] == 1,
                }
            )
    current.sort(key=lambda row: (row["seconds_open"] is None, -(row["seconds_open"] or 0), row["device_id"]))
    counts = {
        str(code): {"status": code, "candidates": 0, "evaluable": 0, "overlapped": 0, "unknown": 0}
        for code in (0, 1, 2, 3, 5)
    }
    overlap_items, seen = [], set()
    for row in orders:
        identity = (row["tenant_id"], row.get("order_no"))
        if not identity[1] or identity in seen:
            quality["duplicate_or_missing_order_ids"] += 1
            continue
        seen.add(identity)
        key = (row["tenant_id"], row["device_id"], row["site_id"])
        status = row.get("status")
        bucket = counts.setdefault(
            str(status), {"status": status, "candidates": 0, "evaluable": 0, "overlapped": 0, "unknown": 0}
        )
        role = role_of(status)
        try:
            began = instant(row.get("created_time"), zone)
            ended = instant(row.get("stop_time"), zone)
            if ended is None and role == StatusRole.CHARGING:
                ended = now
            if began is None or ended is None or ended <= began or role == StatusRole.UNKNOWN:
                raise ValueError("unknown order interval")
            if not max(began, lo) < min(ended, hi):
                continue
        except (ValueError, TypeError):
            bucket["candidates"] += 1
            bucket["unknown"] += 1
            continue
        bucket["candidates"] += 1
        spans = [
            (max(a, began), min(b, ended)) for a, b in intervals.get(key, []) if max(a, began) < min(b, ended)
        ]
        if key in invalid_devices:
            bucket["unknown"] += 1
            continue
        bucket["evaluable"] += 1
        if not spans:
            continue
        bucket["overlapped"] += 1
        overlap_items.append(
            {
                "order_no": row["order_no"],
                "device_id": row["device_id"],
                "site_id": row["site_id"],
                "status": status,
                "overlap_seconds": seconds(spans),
                "record_start": format_stored_time(began),
                "record_end": format_stored_time(ended),
            }
        )
    abnormal = [value for value in counts.values() if role_of(value["status"]) in ABNORMAL]
    evaluable = sum(row["evaluable"] for row in abnormal)
    overlapping = sum(row["overlapped"] for row in abnormal)
    overlap_items.sort(key=lambda row: (row["record_start"], row["order_no"]), reverse=True)
    return {
        "schema": SCHEMA,
        "as_of": format_stored_time(now),
        "window": {"start": format_stored_time(lo), "end": format_stored_time(hi), "timezone": timezone},
        "summary": {
            "new_events": sum(new_count.values()),
            "devices": len(top),
            "offline_seconds": None if invalid_devices else sum(r["offline_seconds"] for r in top),
            "known_offline_seconds": sum(r["offline_seconds"] for r in top),
            "unknown_duration_devices": len(invalid_devices),
        },
        "top_devices": {"total": len(top), "truncated": len(top) > 10, "items": top[:10]},
        "open_incident_list": {
            "as_of": format_stored_time(now),
            "total": len(current),
            "unknown_duration_devices": len(unknown_open),
            "truncated": len(current) > 50,
            "items": current[:50],
        },
        "order_overlap": {
            "abnormal_orders": sum(r["candidates"] for r in abnormal),
            "evaluable": evaluable,
            "unknown": sum(r["unknown"] for r in abnormal),
            "overlapped": overlapping,
            "share": round(overlapping / evaluable, 4) if evaluable else None,
            "by_status": list(counts.values())[:16],
            "total": len(overlap_items),
            "truncated": len(overlap_items) > 50,
            "items": overlap_items[:50],
        },
        "quality": dict(quality),
        "limitations": (
            "平台记录窗口，不代表物理断网或充电起止；未记录恢复不等于实时离线；"
            "时间重叠不能证明故障原因；终态缺结束时间不可判定。"
        ),
    }
