"""单一只读快照下的离线事实；先范围下推，再核对设备当前归属。"""

from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.data.sources import SourceError, _placeholders
from aiops_diagnostics.operator import device_offline

MAX_ROWS = 5000
MAX_DEVICES = 1000


def read(source, site_ids, start, end, as_of, *, device_id="", include_legacy=False):
    timezone = source.settings.mysql.timezone
    if not timezone:
        raise SourceError("离线分析尚未声明业务数据时区")
    lo, hi, now, zone = device_offline.window(start, end, as_of, timezone)
    if source.scope is None or not source.scope.tenant_id or source.scope.user_id:
        raise SourceError("离线分析需要明确租户与管家站点范围")
    sites = source._bounded_sites(site_ids)
    if source._scope_blocked() or sites == ():
        raise SourceError("离线分析范围为空")
    where, params = "m.tenant_id=%s", [source.scope.tenant_id]
    if sites is not None:
        where += f" AND m.site_id IN ({_placeholders(len(sites))})"
        params.extend(sites)
    if device_id:
        where += " AND m.device_id=%s"
        params.append(device_id)
    bounds = [value.astimezone(zone).replace(tzinfo=None) for value in (lo, hi, now)]
    joins = (
        " LEFT JOIN iot_charging_device d ON d.id=m.device_id"
        " AND d.tenant_id=m.tenant_id AND d.site_id=m.site_id"
        " LEFT JOIN ch_site s ON s.id=m.site_id AND s.tenant_id=m.tenant_id "
    )
    with source._cursor() as cur:
        cur.execute("SELECT @@transaction_isolation AS isolation_level")
        if cur.fetchone()["isolation_level"] != "REPEATABLE-READ":
            raise SourceError("离线分析需要一致性读取")
        cur.execute(
            "SELECT m.id,m.site_id,m.device_code FROM iot_charging_device m WHERE "
            + where.replace("m.device_id", "m.id")
            + " ORDER BY m.id LIMIT %s",
            [*params, MAX_DEVICES + 1],
        )
        devices = cur.fetchall()
        if len(devices) > MAX_DEVICES:
            raise SourceError("离线分析设备超过上限，请缩小站点范围")
        if not devices:
            raise SourceError("没有可见设备")
        device_ids = [d["id"] for d in devices]
        cur.execute("SHOW INDEX FROM iot_device_offline_monitor")
        indexes = cur.fetchall()
        if not any(
            r["Key_name"] == "aiops_offline_device_time"
            and r["Column_name"] == "device_id"
            and r["Seq_in_index"] == 1
            for r in indexes
        ):
            raise SourceError("离线分析缺少设备归属查询索引，请联系管理员完成部署")
        cur.execute(
            "SELECT m.id,m.tenant_id,m.device_id,d.device_code,m.site_id,m.offline_time,m.recover_time,"
            "d.id AS verified_device,d.online_status,s.name AS site_name FROM iot_device_offline_monitor m "
            "FORCE INDEX (aiops_offline_device_time)"
            + joins
            + "WHERE "
            + where
            + f" AND m.device_id IN ({_placeholders(len(device_ids))})"
            + " AND (m.offline_time IS NULL OR (m.offline_time<=%s AND (m.recover_time IS NULL"
            " OR m.recover_time>=%s OR m.recover_time<=m.offline_time))) ORDER BY m.id LIMIT %s",
            [*params, *device_ids, bounds[2], bounds[0], MAX_ROWS + 1],
        )
        events = cur.fetchall()
        cur.execute(
            "SELECT m.order_no,m.tenant_id,m.device_id,m.site_id,m.created_time,m.stop_time,m.status,"
            "d.id AS verified_device FROM ch_order_info m FORCE INDEX (ch_order_info_device_id_index)"
            + joins
            + "WHERE "
            + where
            + f" AND m.device_id IN ({_placeholders(len(device_ids))})"
            + " AND (m.created_time IS NULL OR m.created_time<%s)"
            " AND (m.stop_time IS NULL OR m.stop_time>=%s OR m.stop_time<=m.created_time)"
            " ORDER BY m.order_no LIMIT %s",
            [*params, *device_ids, bounds[1], bounds[0], MAX_ROWS + 1],
        )
        orders = cur.fetchall()
        if len(events) > MAX_ROWS or len(orders) > MAX_ROWS:
            raise SourceError("离线分析数据超过单次上限，请缩小站点或设备范围")
        legacy = None
        if include_legacy:
            # 兼容卡原有摘要；不能把当前设备过滤后的新统计冒充旧字段。
            legacy_start, legacy_end = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
            cur.execute(
                "SELECT COUNT(*) incidents,COUNT(DISTINCT m.device_id) offline_devices,"
                "SUM(m.recover_time IS NULL) open_incidents,SUM(CASE WHEN m.recover_time IS NULL THEN 0"
                " ELSE GREATEST(TIMESTAMPDIFF(MINUTE,m.offline_time,LEAST(m.recover_time,%s)),0) END)"
                " offline_minutes FROM iot_device_offline_monitor m WHERE "
                + where
                + " AND m.offline_time>=%s AND m.offline_time<%s",
                [legacy_end, *params, legacy_start, legacy_end],
            )
            legacy = {k: int(v or 0) for k, v in cur.fetchone().items()}
    rejected = sum(not row["verified_device"] for row in [*events, *orders])
    result = device_offline.analyze(
        [r for r in events if r["verified_device"]],
        [r for r in orders if r["verified_device"]],
        start=format_stored_time(lo),
        end=format_stored_time(hi),
        as_of=now,
        timezone=timezone,
    )
    result["quality"]["unverified_device_records"] = rejected
    result["quality"]["coverage"] = "partial" if rejected else "current_devices"
    if result["quality"]["coverage"] == "partial":
        result["order_overlap"]["share"] = None
    result["limitations"] += " 仅覆盖当前可授权设备；已删除设备或历史归属变化可能缺记录。"
    result["scope"] = {"site_count": len(sites) if sites is not None else None, "device_id": device_id}
    result["references"] = {
        "devices": sorted({(r["id"], r["site_id"]) for r in devices}),
        "orders": sorted(
            {
                (r["order_no"], r["device_id"], r["site_id"])
                for r in orders
                if r["verified_device"] and r["order_no"]
            }
        ),
    }
    return legacy, result


def require_references(source, references):
    """历史数字也依赖当前对象范围；仅核归属，不重新计算历史结果。"""
    if source.scope is None or not source.scope.tenant_id or source.scope.user_id or source._scope_blocked():
        raise SourceError("离线分析范围已改变")
    allowed = source.scope.site_ids
    with source._cursor() as cur:
        for kind, table, fields in (
            ("devices", "iot_charging_device", "id,site_id"),
            ("orders", "ch_order_info", "order_no,device_id,site_id"),
        ):
            rows = references.get(kind, [])
            if len(rows) > MAX_ROWS or any(allowed is not None and r[-1] not in allowed for r in rows):
                raise SourceError("离线分析范围已改变")
            if not rows:
                continue
            ids = sorted({row[0] for row in rows})
            cur.execute(
                f"SELECT {fields} FROM {table} WHERE tenant_id=%s AND {fields.split(',')[0]}"
                f" IN ({_placeholders(len(ids))}) LIMIT %s",
                [source.scope.tenant_id, *ids, MAX_ROWS + 1],
            )
            actual = {tuple(row[f] for f in fields.split(",")) for row in cur.fetchall()}
            if {tuple(row) for row in rows} != actual:
                raise SourceError("离线分析对象归属已改变")


def resolve_device(source, value, site_ids):
    sites = source._bounded_sites(site_ids)
    if (
        source.scope is None
        or not source.scope.tenant_id
        or source.scope.user_id
        or source._scope_blocked()
        or sites == ()
    ):
        raise SourceError("离线分析范围为空")
    where, params = "tenant_id=%s AND (id=%s OR device_code=%s)", [source.scope.tenant_id, value, value]
    if sites is not None:
        where += f" AND site_id IN ({_placeholders(len(sites))})"
        params.extend(sites)
    with source._cursor() as cur:
        cur.execute("SELECT id FROM iot_charging_device WHERE " + where + " LIMIT 2", params)
        rows = cur.fetchall()
    if len(rows) != 1:
        raise SourceError("请提供唯一可见设备的编号")
    return rows[0]["id"]
