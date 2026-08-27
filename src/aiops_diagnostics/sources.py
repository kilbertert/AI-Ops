from __future__ import annotations

import base64
import contextlib
import copy
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import pymysql
import redis
from pymysql.cursors import DictCursor

from aiops_diagnostics.config import Settings
from aiops_diagnostics.http_auth import build_internal_token_headers

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SourceError(RuntimeError):
    """A bounded read-only data-source operation failed."""


class DiagnosticSources(Protocol):
    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_fee_template_record(
        self, order_no: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]: ...

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]: ...

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]: ...

    def doctor(self) -> dict[str, Any]: ...


ORDER_COLUMNS = """
id, order_no, tenant_id, status, type, billing_type, launch_type, is_test,
device_id, device_code, child_device_id, child_device_code, site_id,
electricity, electricity_fee, service_fee, ds_electric_fee, ds_service_fee,
tip_electricity, tip_fee, tip_electricity_fee, tip_service_fee,
peak_electricity, peak_fee, peak_electricity_fee, peak_service_fee,
flat_electricity, flat_fee, flat_electricity_fee, flat_service_fee,
valley_electricity, valley_fee, valley_electricity_fee, valley_service_fee,
ds_tip_electricity, ds_tip_fee, ds_peak_electricity, ds_peak_fee,
ds_flat_electricity, ds_flat_fee, ds_valley_electricity, ds_valley_fee,
ds_electricity, ds_fee, has_electricity_loss, fee_template_id,
launch_fee, park_fee, occupy_fee, appointment_fee, insurance_amount,
market_amount, platform_amount, pay_amount, total_amount, reduce_balance,
is_pay, is_receive_tx_data, sync_mall_order, out_trade_no,
stopped_reason_code, stopped_reason_content, error_time, error_info,
last_report_amount, transaction_id, meter_start, meter_end,
white_flag, balance_insufficient_stop, start_soc, end_soc, device_protocol,
created_time, stop_time, draw_gun_time, tx_data
""".replace("\n", " ").strip()


class MySQLSource:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = _safe_identifier(settings.mysql.database)

    @contextlib.contextmanager
    def _cursor(self) -> Iterator[DictCursor]:
        cfg = self.settings.mysql
        if not cfg.user or not cfg.password:
            raise SourceError("MySQL 只读账号未配置")
        try:
            connection = pymysql.connect(
                host=cfg.host,
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                database=cfg.database,
                charset="utf8mb4",
                connect_timeout=self.settings.safety.query_timeout_seconds,
                read_timeout=self.settings.safety.query_timeout_seconds,
                write_timeout=self.settings.safety.query_timeout_seconds,
                cursorclass=DictCursor,
                autocommit=False,
            )
        except Exception as exc:
            raise SourceError(f"MySQL 连接失败: {exc.__class__.__name__}") from exc
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SET SESSION MAX_EXECUTION_TIME={int(self.settings.safety.mysql_max_execution_ms)}"
                )
                cursor.execute("START TRANSACTION READ ONLY")
                yield cursor
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"MySQL 只读查询失败: {exc.__class__.__name__}") from exc
        finally:
            with contextlib.suppress(Exception):
                connection.rollback()
            with contextlib.suppress(Exception):
                connection.close()

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        where = "order_no=%s"
        params: list[Any] = [order_no]
        if tenant_id:
            where += " AND tenant_id=%s"
            params.append(tenant_id)
        sql = (
            f"SELECT /*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */ "
            f"{ORDER_COLUMNS} FROM `{self.database}`.`ch_order_info` WHERE {where} "
            "ORDER BY created_time DESC LIMIT 3"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            return [_normalize_row(row) for row in cursor.fetchall()]

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        where = "order_no=%s"
        params: list[Any] = [order_no]
        if tenant_id:
            where += " AND tenant_id=%s"
            params.append(tenant_id)
        sql = (
            f"SELECT order_no, tenant_id, fee_template, occupy_fee_template, period_fee_detail "
            f"FROM `{self.database}`.`ch_fee_template_record` WHERE {where} "
            "ORDER BY created_time DESC LIMIT 1"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return _normalize_row(row) if row else None

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        if device_id:
            where = "id=%s"
            value = device_id
        elif device_code:
            where = "device_code=%s"
            value = device_code
        else:
            return None
        params: list[Any] = [value]
        if tenant_id:
            where += " AND tenant_id=%s"
            params.append(tenant_id)
        sql = (
            f"SELECT id, tenant_id, site_id, device_code, protocol, online_status, status, work_status, "
            f"error_reason, fee_template_id FROM `{self.database}`.`iot_charging_device` "
            f"WHERE {where} LIMIT 1"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return _normalize_row(row) if row else None

    def doctor(self) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT VERSION() AS version, CURRENT_USER() AS `current_user`, @@time_zone AS time_zone"
            )
            details = _normalize_row(cursor.fetchone())
            cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
            grant_rows = [str(value) for row in cursor.fetchall() for value in row.values()]
            assigned_roles, has_unparsed_role_assignment = _assigned_roles(grant_rows)
            unresolved_roles: list[str] = ["ASSIGNMENT"] if has_unparsed_role_assignment else []
            if assigned_roles:
                try:
                    cursor.execute(f"SHOW GRANTS FOR CURRENT_USER() USING {', '.join(assigned_roles)}")
                    grant_rows.extend(str(value) for row in cursor.fetchall() for value in row.values())
                except Exception:
                    unresolved_roles.extend(assigned_roles)
            granted_privileges: set[str] = set()
            for grant in grant_rows:
                grant_upper = grant.upper()
                match = re.search(r"\bGRANT\s+(.+?)\s+ON\s+", grant_upper)
                if match:
                    granted_privileges.update(item.strip() for item in match.group(1).split(","))
                if "WITH GRANT OPTION" in grant_upper:
                    granted_privileges.add("GRANT OPTION")
            granted_privileges.update(f"UNRESOLVED ROLE {role}" for role in unresolved_roles)
            unsafe_privileges = granted_privileges.difference({"SELECT", "SHOW VIEW", "USAGE"})
            details["read_only"] = not unsafe_privileges
            details["unsafe_privileges"] = sorted(unsafe_privileges)
            details["assigned_roles"] = assigned_roles
            return details


class TDengineSource:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = _safe_identifier(settings.tdengine.database)

    def _query(self, sql: str) -> list[dict[str, Any]]:
        cfg = self.settings.tdengine
        if not cfg.user or not cfg.password:
            raise SourceError("TDengine 只读账号未配置")
        endpoint = f"{cfg.url.rstrip('/')}/rest/sql/{self.database}"
        request = urllib.request.Request(endpoint, data=sql.encode("utf-8"), method="POST")
        token = base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Content-Type", "text/plain; charset=utf-8")
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.safety.query_timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SourceError(f"TDengine 查询失败: {exc.__class__.__name__}") from exc
        if payload.get("code") not in (None, 0):
            raise SourceError(f"TDengine 拒绝查询: {payload.get('desc', 'unknown error')}")
        columns = [item[0] if isinstance(item, list) else item for item in payload.get("column_meta", [])]
        return [dict(zip(columns, row, strict=False)) for row in payload.get("data") or []]

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]:
        device_literal = _safe_literal(device)
        tx_filter = f" AND `txSerialNo`='{_safe_literal(tx_serial_no)}'" if tx_serial_no else ""
        limit = int(self.settings.safety.tdengine_max_rows)
        sql = (
            "SELECT _ts, `txSerialNo`, status, `isReturn`, `isInsert`, `outputVoltage`, "
            "`outputCurrent`, power, `chargingTime`, `chargingElectricityQuantity`, soc, temperature, "
            "`batteryMaxTemperature`, `batteryMinTemperature`, `errorCode`, `errorReason`, `meterNow` "
            "FROM `charging-gun_property` "
            f"WHERE device='{device_literal}' AND _ts>='{_format_time(start_time)}' "
            f"AND _ts<='{_format_time(end_time)}'{tx_filter} ORDER BY _ts ASC LIMIT {limit}"
        )
        return self._query(sql)

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        limit = int(self.settings.safety.tdengine_max_rows)
        sql = (
            "SELECT _ts, direction, code, decoded FROM `charging-pile_comm` "
            f"WHERE device='{_safe_literal(device)}' AND _ts>='{_format_time(start_time)}' "
            f"AND _ts<='{_format_time(end_time)}' ORDER BY _ts ASC LIMIT {limit}"
        )
        return self._query(sql)

    def doctor(self) -> dict[str, Any]:
        rows = self._query("SHOW STABLES")
        names = {str(value) for row in rows for value in row.values()}
        return {
            "database": self.database,
            "charging_gun_property": "charging-gun_property" in names,
            "charging_pile_comm": "charging-pile_comm" in names,
            "stable_count": len(rows),
        }


class HttpSources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api = settings.http

    def _headers(self) -> dict[str, str]:
        return build_internal_token_headers(
            self.api.internal_token.secret,
            self.api.internal_token.expire_seconds,
            int(time.time()),
        )

    def _get(self, endpoint: str, params: dict[str, str]) -> Any:
        if not self.api.base_url:
            raise SourceError("Diag API 地址未配置")
        if not self.api.internal_token.secret:
            raise SourceError("Diag API 内部令牌密钥未配置")
        if not self.api.internal_token.expire_seconds:
            raise SourceError("Diag API 内部令牌有效期未配置")
        url = self.api.base_url.rstrip("/") + endpoint
        if params:
            url = f"{url}?{urlencode(sorted(params.items()))}"
        request = urllib.request.Request(url, method="GET")
        for name, value in self._headers().items():
            request.add_header(name, value)
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.api.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _http_error_message(exc)
            if exc.code in (401, 403):
                raise SourceError(detail or "Diag API 令牌无效或过期") from exc
            raise SourceError(f"Diag API 请求失败: {detail or f'HTTP {exc.code}'}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceError(f"Diag API 请求失败: {exc.__class__.__name__}") from exc
        if not isinstance(payload, dict) or payload.get("code") not in (0, 200):
            detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
            raise SourceError(f"Diag API 拒绝查询: {detail}")
        return payload.get("data")

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        params = {"order_no": _safe_http_param(order_no), "include_fee_template": "false"}
        if tenant_id:
            params["tenant_id"] = _safe_http_param(tenant_id)
        data = self._get("/diag/order", params)
        return copy.deepcopy(data.get("orders") or []) if isinstance(data, dict) else []

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        params = {"order_no": _safe_http_param(order_no), "include_fee_template": "true"}
        if tenant_id:
            params["tenant_id"] = _safe_http_param(tenant_id)
        data = self._get("/diag/order", params)
        if not isinstance(data, dict):
            return None
        return copy.deepcopy(data.get("fee_template"))

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, str] = {}
        if device_id:
            params["device_id"] = _safe_http_param(device_id)
        elif device_code:
            params["device_code"] = _safe_http_param(device_code)
        else:
            return None
        if tenant_id:
            params["tenant_id"] = _safe_http_param(tenant_id)
        data = self._get("/diag/device", params)
        return copy.deepcopy(data) if isinstance(data, dict) else None

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]:
        params = {
            "device": _safe_http_param(device),
            "start_time": _format_http_time(start_time),
            "end_time": _format_http_time(end_time),
        }
        if tx_serial_no:
            params["tx_serial_no"] = _safe_http_param(tx_serial_no)
        data = self._get("/diag/gun-property", params)
        return copy.deepcopy(data) if isinstance(data, list) else []

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "device": _safe_http_param(device),
            "start_time": _format_http_time(start_time),
            "end_time": _format_http_time(end_time),
        }
        data = self._get("/diag/comm-message", params)
        return copy.deepcopy(data) if isinstance(data, list) else []

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]:
        data = self._get("/diag/redis-stream", {"order_no": _safe_http_param(order_no)})
        return copy.deepcopy(data) if isinstance(data, list) else []

    def doctor(self) -> dict[str, Any]:
        return {
            "diag_api": {
                "ok": bool(
                    self.api.base_url
                    and self.api.internal_token.secret
                    and self.api.internal_token.expire_seconds
                ),
                "details": {
                    "base_url": self.api.base_url,
                    "token_configured": bool(self.api.internal_token.secret),
                    "token_expire_seconds": self.api.internal_token.expire_seconds,
                },
            }
        }


class HybridSources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpSources(settings)
        self.tdengine = TDengineSource(settings)

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return self.http.get_orders(order_no, tenant_id)

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.http.get_fee_template_record(order_no, tenant_id)

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self.http.get_device(device_id, device_code, tenant_id)

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]:
        return self.tdengine.get_gun_samples(device, start_time, end_time, tx_serial_no)

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return self.tdengine.get_comm_messages(device, start_time, end_time)

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]:
        return self.http.inspect_streams(order_no)

    def doctor(self) -> dict[str, Any]:
        http = self.http.doctor()
        try:
            details = self.tdengine.doctor()
        except SourceError as exc:
            return {"diag_api": http["diag_api"], "tdengine": {"ok": False, "error": str(exc)}}
        return {"diag_api": http["diag_api"], "tdengine": {"ok": True, "details": details}}


class RedisSource:
    STREAMS = ("third.order.sync.queue", "third.order.sync.notify.queue")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self) -> redis.Redis:
        cfg = self.settings.redis
        if not cfg.password:
            raise SourceError("Redis 只读账号未配置")
        return redis.Redis(
            host=cfg.host,
            port=cfg.port,
            db=cfg.database,
            username=cfg.user or None,
            password=cfg.password,
            socket_connect_timeout=self.settings.safety.query_timeout_seconds,
            socket_timeout=self.settings.safety.query_timeout_seconds,
            decode_responses=False,
        )

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]:
        client = self._client()
        results: list[dict[str, Any]] = []
        try:
            for stream in self.STREAMS:
                stream_key = stream.encode("utf-8")
                stream_type = _decode_text(client.type(stream_key))
                if stream_type != "stream":
                    results.append(
                        {"stream": stream, "type": stream_type, "length": 0, "groups": [], "matches": 0}
                    )
                    continue
                groups = client.xinfo_groups(stream_key)
                messages = client.xrevrange(stream_key, count=self.settings.safety.redis_max_messages)
                matches = sum(_stream_fields_contain(fields, order_no) for _, fields in messages)
                results.append(
                    {
                        "stream": stream,
                        "type": stream_type,
                        "length": client.xlen(stream_key),
                        "groups": [
                            {
                                "name": _decode_text(_mapping_get(group, "name")),
                                "consumers": _mapping_get(group, "consumers"),
                                "pending": _mapping_get(group, "pending"),
                                "lag": _mapping_get(group, "lag"),
                            }
                            for group in groups
                        ],
                        "inspected_messages": len(messages),
                        "matches": matches,
                    }
                )
        except Exception as exc:
            raise SourceError(f"Redis 只读查询失败: {exc.__class__.__name__}") from exc
        finally:
            client.close()
        return results

    def doctor(self) -> dict[str, Any]:
        client = self._client()
        try:
            info = client.info("server")
            return {
                "ping": client.ping(),
                "version": _decode_text(_mapping_get(info, "redis_version")),
                "mode": _decode_text(_mapping_get(info, "redis_mode")),
                "database": self.settings.redis.database,
            }
        except Exception as exc:
            raise SourceError(f"Redis 连接失败: {exc.__class__.__name__}") from exc
        finally:
            client.close()


@dataclass(slots=True)
class LiveSources:
    mysql: MySQLSource
    tdengine: TDengineSource
    redis: RedisSource

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return self.mysql.get_orders(order_no, tenant_id)

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.mysql.get_fee_template_record(order_no, tenant_id)

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self.mysql.get_device(device_id, device_code, tenant_id)

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]:
        return self.tdengine.get_gun_samples(device, start_time, end_time, tx_serial_no)

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return self.tdengine.get_comm_messages(device, start_time, end_time)

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]:
        return self.redis.inspect_streams(order_no)

    def doctor(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, source in (("mysql", self.mysql), ("tdengine", self.tdengine), ("redis", self.redis)):
            try:
                details = source.doctor()
                if name == "mysql" and details.get("read_only") is False:
                    result[name] = {
                        "ok": False,
                        "error": "MySQL 账号包含写权限，拒绝作为诊断运行时账号",
                        "details": details,
                    }
                else:
                    result[name] = {"ok": True, "details": details}
            except SourceError as exc:
                result[name] = {"ok": False, "error": str(exc)}
        return result


class FixtureSources:
    def __init__(self, path: Path) -> None:
        self.payload = json.loads(path.read_text(encoding="utf-8"))

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        orders = [row for row in self.payload.get("orders", []) if row.get("order_no") == order_no]
        if tenant_id:
            orders = [row for row in orders if row.get("tenant_id") == tenant_id]
        return copy.deepcopy(orders)

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        record = self.payload.get("fee_template_records", {}).get(order_no)
        if record and tenant_id and record.get("tenant_id") != tenant_id:
            return None
        return copy.deepcopy(record)

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        for device in self.payload.get("devices", []):
            if (
                (device_id and device.get("id") == device_id)
                or (device_code and device.get("device_code") == device_code)
            ) and (not tenant_id or not device.get("tenant_id") or device.get("tenant_id") == tenant_id):
                return copy.deepcopy(device)
        return None

    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(self.payload.get("gun_samples", []))

    def get_comm_messages(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(self.payload.get("comm_messages", []))

    def inspect_streams(self, order_no: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.payload.get("streams", []))

    def doctor(self) -> dict[str, Any]:
        return {"fixture": {"ok": True, "details": {"synthetic": True}}}


@contextlib.contextmanager
def direct_sources(settings: Settings) -> Iterator[LiveSources]:
    """Keep the all-direct source set available for explicit rollback/fallback."""
    with _ssh_tunnel(settings) as effective:
        yield LiveSources(
            mysql=MySQLSource(effective),
            tdengine=TDengineSource(effective),
            redis=RedisSource(effective),
        )


@contextlib.contextmanager
def live_sources(settings: Settings) -> Iterator[HybridSources]:
    """Return the current partial-cutover default source set."""
    with _ssh_tunnel(settings) as effective:
        yield HybridSources(effective)


@contextlib.contextmanager
def _ssh_tunnel(settings: Settings) -> Iterator[Settings]:
    if not settings.ssh.enabled:
        yield settings
        return
    settings.ssh.validate()
    local_mysql, local_tdengine, local_redis = (_available_port() for _ in range(3))
    ssh = settings.ssh
    command = [
        ssh.ssh_bin,
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-p",
        str(ssh.port),
        "-i",
        str(Path(ssh.key_file).expanduser()),
        "-L",
        f"127.0.0.1:{local_mysql}:{ssh.mysql_host}:{ssh.mysql_port}",
        "-L",
        f"127.0.0.1:{local_tdengine}:{ssh.tdengine_host}:{ssh.tdengine_port}",
        "-L",
        f"127.0.0.1:{local_redis}:{ssh.redis_host}:{ssh.redis_port}",
        f"{ssh.user}@{ssh.host}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for port in (local_mysql, local_tdengine, local_redis):
            _wait_for_port(port, process, settings.safety.query_timeout_seconds)
        effective = copy.deepcopy(settings)
        effective.mysql = replace(effective.mysql, host="127.0.0.1", port=local_mysql)
        effective.tdengine = replace(effective.tdengine, url=f"http://127.0.0.1:{local_tdengine}")
        effective.redis = replace(effective.redis, host="127.0.0.1", port=local_redis)
        yield effective
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        if process.poll() is None:
            process.kill()


def _wait_for_port(port: int, process: subprocess.Popen[str], timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().strip() if process.stderr else ""
            raise SourceError(f"SSH 隧道启动失败: {stderr or 'ssh exited'}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise SourceError("SSH 隧道启动超时")


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _safe_identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"非法数据库标识符: {value!r}")
    return value


def _safe_literal(value: str | None) -> str:
    if value is None or not SAFE_VALUE.fullmatch(value) or "--" in value:
        raise ValueError("查询值包含不允许的字符")
    return value


SAFE_HTTP_PARAM = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")


def _safe_http_param(value: str | None) -> str:
    """Whitelist query values before they are interpolated into ``/diag/*`` URLs."""
    if value is None or not SAFE_HTTP_PARAM.fullmatch(value) or "--" in value:
        raise ValueError("HTTP 查询值包含不允许的字符")
    return value


_ROLE_ACCOUNT = re.compile(r"`(?P<name>[A-Za-z0-9_.:-]{1,128})`@`(?P<host>[A-Za-z0-9_.:%*-]{1,255})`")


def _assigned_roles(grants: list[str]) -> tuple[list[str], bool]:
    roles: list[str] = []
    has_unparsed_assignment = False
    for grant in grants:
        match = re.match(r"\s*GRANT\s+(.+?)\s+TO\s+", grant, re.IGNORECASE)
        if not match or re.search(r"\bON\b", match.group(1), re.IGNORECASE):
            continue
        role_text = match.group(1)
        tokens = list(_ROLE_ACCOUNT.finditer(role_text))
        compact_roles = ",".join(token.group(0) for token in tokens)
        if not tokens or compact_roles != re.sub(r"\s+", "", role_text):
            has_unparsed_assignment = True
            continue
        roles.extend(token.group(0) for token in tokens)
    return list(dict.fromkeys(roles)), has_unparsed_assignment


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _format_http_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    with contextlib.suppress(Exception):
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("msg"):
            return str(payload["msg"])
    return ""


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value[:1] in {"{", "["}:
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        normalized[str(key)] = value
    return normalized


def _mapping_get(mapping: dict[Any, Any], key: str) -> Any:
    return mapping.get(key, mapping.get(key.encode("utf-8")))


def _decode_text(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _stream_fields_contain(fields: dict[Any, Any], token: str) -> bool:
    token_bytes = token.encode("utf-8")
    for key, value in fields.items():
        for candidate in (key, value):
            if isinstance(candidate, (bytes, bytearray)):
                if token_bytes in bytes(candidate):
                    return True
            elif token in str(candidate):
                return True
    return False
