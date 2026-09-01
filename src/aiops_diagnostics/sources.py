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
from collections.abc import Callable, Iterator, Mapping
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
from aiops_diagnostics.query_scope import QueryScope

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_HTTP_ERROR_CONFIG_MISSING = "http.config_missing"
_HTTP_ERROR_AUTH_FAILED = "http.auth_failed"
_HTTP_ERROR_UNREACHABLE = "http.http_unreachable"

_HTTP_ERROR_MESSAGES = {
    _HTTP_ERROR_CONFIG_MISSING: "Diag HTTP 配置不完整",
    _HTTP_ERROR_AUTH_FAILED: "Diag HTTP 鉴权失败",
    _HTTP_ERROR_UNREACHABLE: "Diag HTTP 服务不可达或响应异常",
}


class SourceError(RuntimeError):
    """A bounded read-only data-source operation failed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class DiagnosticSources(Protocol):
    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]: ...

    def get_fee_template_record(
        self, order_no: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def get_occupy_orders(
        self,
        order_id: str | None = None,
        order_no: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

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

OCCUPY_ORDER_COLUMNS = """
id, orderId, order_no, device_id, device_code, child_device_id,
child_device_code, site_id, userId, free_time, timeout, occupy_amount,
pay_amount, status, out_trade_no, is_pay, is_sync_mall_order, pay_time,
tenant_id, startTime, endTime, operator_id, refund_status, refund_amount,
refund_time, refundRemark
""".replace("\n", " ").strip()

OCCUPY_ORDER_LIMIT = 20

#: 站点归属解析（ch_site）单次返回行数上限。
SITE_SCOPE_MAX_ROWS = 1000


def _placeholders(count: int) -> str:
    return ", ".join("%s" for _ in range(count))


class MySQLSource:
    """直连 MySQL 诊断查询；可选携带单次运行冻结的 ``QueryScope``。

    携带 ``scope`` 时，租户/站点/用户范围以 SQL 过滤条件下推（参数绑定），
    调用方传入的 ``tenant_id`` 被忽略，不信任前端裸传的权限字段；最终可见
    站点为空时所有范围查询短路返回空结果，不发起 SQL。
    """

    def __init__(self, settings: Settings, scope: QueryScope | None = None) -> None:
        self.settings = settings
        self.scope = scope
        self.database = _safe_identifier(settings.mysql.database)

    def _scope_where(
        self,
        *,
        site_column: str | None = "site_id",
        user_column: str | None = "user_id",
    ) -> tuple[str, list[Any]]:
        """构造 scope 下推的 WHERE 片段（不含前导 AND，调用方自行拼接）。

        站点/用户列名来自固定表结构常量，不是客户端输入；值一律参数绑定。
        """
        scope = self.scope
        if scope is None:
            return "", []
        fragments: list[str] = ["tenant_id=%s"]
        params: list[Any] = [scope.tenant_id]
        if scope.site_ids is not None and site_column:
            fragments.append(f"{site_column} IN ({_placeholders(len(scope.site_ids))})")
            params.extend(scope.site_ids)
        if scope.user_id and user_column:
            fragments.append(f"{user_column}=%s")
            params.append(scope.user_id)
        return " AND ".join(fragments), params

    def _scope_blocked(self) -> bool:
        """最终可见站点为空：短路返回空结果，不发起 SQL。"""
        return self.scope is not None and self.scope.empty_site_scope

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
        if self._scope_blocked():
            return []
        where = "order_no=%s"
        params: list[Any] = [order_no]
        scope_where, scope_params = self._scope_where()
        if scope_where:
            where += f" AND {scope_where}"
            params.extend(scope_params)
        elif tenant_id:
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
        if self._scope_blocked():
            return None
        scope_where, scope_params = self._scope_where()
        sql = (
            f"SELECT order_no, tenant_id, fee_template, occupy_fee_template, period_fee_detail "
            f"FROM `{self.database}`.`ch_fee_template_record` WHERE order_no=%s "
        )
        params: list[Any] = [order_no]
        if scope_where:
            # 计费模板表没有站点/用户列；先用与订单完全相同的范围谓词做订单
            # 存在性检查，保证单独查询不会扩大可见范围。
            assert self.scope is not None
            exists_sql = (
                f"SELECT 1 FROM `{self.database}`.`ch_order_info` WHERE order_no=%s AND {scope_where} LIMIT 1"
            )
            fee_sql = sql + "AND tenant_id=%s ORDER BY created_time DESC LIMIT 1"
            fee_params: list[Any] = [order_no, self.scope.tenant_id]
            with self._cursor() as cursor:
                cursor.execute(exists_sql, [order_no, *scope_params])
                if cursor.fetchone() is None:
                    return None
                cursor.execute(fee_sql, fee_params)
                row = cursor.fetchone()
                return _normalize_row(row) if row else None
        if tenant_id:
            sql += "AND tenant_id=%s "
            params.append(tenant_id)
        sql += "ORDER BY created_time DESC LIMIT 1"
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return _normalize_row(row) if row else None

    def get_occupy_orders(
        self,
        order_id: str | None = None,
        order_no: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """占位费订单：``order_id``（充电订单主键关联）与 ``order_no`` 二选一。

        关联键与业务侧一致：``orderId`` 存充电订单 ``ch_order_info.id``，
        ``order_no`` 为业务编号（源码 ``OccupyOrderTxDataHandler``）。
        """
        has_order_id = bool(order_id and order_id.strip())
        has_order_no = bool(order_no and order_no.strip())
        if has_order_id == has_order_no:
            raise ValueError("占位费订单查询必须且只能提供 order_id 或 order_no 之一")
        if self._scope_blocked():
            return []
        where = "orderId=%s" if has_order_id else "order_no=%s"
        params: list[Any] = [order_id if has_order_id else order_no]
        scope_where, scope_params = self._scope_where(user_column="userId")
        if scope_where:
            where += f" AND {scope_where}"
            params.extend(scope_params)
        elif tenant_id:
            where += " AND tenant_id=%s"
            params.append(tenant_id)
        sql = (
            f"SELECT {OCCUPY_ORDER_COLUMNS} FROM `{self.database}`.`ch_occupy_order_info` "
            f"WHERE {where} ORDER BY startTime DESC LIMIT {OCCUPY_ORDER_LIMIT}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            return [_normalize_row(row) for row in cursor.fetchall()]

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
        if self._scope_blocked():
            return None
        params: list[Any] = [value]
        scope_where, scope_params = self._scope_where(user_column=None)
        if scope_where:
            where += f" AND {scope_where}"
            params.extend(scope_params)
        elif tenant_id:
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

    def site_ids_by_shops(self, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        """站点归属解析：``ch_site.shop_id`` → 站点 ID（只读、参数绑定、有界）。"""
        if not shop_ids:
            return ()
        return self._site_ids_by_column("shop_id", shop_ids, tenant_id)

    def site_ids_by_points(self, point_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        """站点归属解析：``ch_site.dis_point_id`` → 站点 ID（Dis 点位归属）。"""
        if not point_ids:
            return ()
        return self._site_ids_by_column("dis_point_id", point_ids, tenant_id)

    def _site_ids_by_column(self, column: str, values: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        if not all(SAFE_VALUE.fullmatch(value) for value in values):
            raise ValueError("范围 ID 包含不允许的字符")
        sql = (
            f"SELECT id FROM `{self.database}`.`ch_site` "
            f"WHERE tenant_id=%s AND {column} IN ({_placeholders(len(values))}) "
            f"ORDER BY id LIMIT {SITE_SCOPE_MAX_ROWS}"
        )
        params: list[Any] = [tenant_id, *values]
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return tuple(str(row["id"]) for row in rows if row.get("id") is not None)

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
    """TDengine 只读查询；可选携带单次运行冻结的允许设备集合。

    携带 ``allowed_devices``（非空）时，每个查询的 ``device`` 必须在集合内，
    否则拒绝且不发起 TDengine 请求。``None`` 表示由上游订单 scope 已约束
    设备（订单本身来自受信任的 MySQL scope 查询），但仍走固定的超表/字段/
    时间窗/LIMIT 构造，不接受客户端透传表名、字段或 SQL。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allowed_devices: frozenset[str] | None = None,
    ) -> None:
        self.settings = settings
        self.allowed_devices = allowed_devices
        self.database = _safe_identifier(settings.tdengine.database)

    def _check_device_allowed(self, device: str) -> None:
        """请求设备不在允许集合内时 fail closed：拒绝且不发起 TDengine 请求。"""
        if self.allowed_devices is not None and device not in self.allowed_devices:
            raise SourceError(
                f"设备不在当前权限范围: {device}",
                code="tdengine.device_forbidden",
            )

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
        self._check_device_allowed(device)
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
        self._check_device_allowed(device)
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
        if not self.api.internal_token.secret:
            raise SourceError("Diag API 内部令牌密钥未配置")

    def _headers(self) -> dict[str, str]:
        return build_internal_token_headers(
            self.api.internal_token.secret,
            self.api.internal_token.expire_seconds,
            int(time.time()),
        )

    def _get(self, endpoint: str, params: dict[str, str]) -> Any:
        if not self.api.base_url:
            raise SourceError("Diag API 地址未配置", code=_HTTP_ERROR_CONFIG_MISSING)
        if not self.api.internal_token.secret:
            raise SourceError("Diag API 内部令牌密钥未配置", code=_HTTP_ERROR_CONFIG_MISSING)
        if not self.api.internal_token.expire_seconds:
            raise SourceError("Diag API 内部令牌有效期未配置", code=_HTTP_ERROR_CONFIG_MISSING)
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
                raise SourceError(detail or "Diag API 令牌无效或过期", code=_HTTP_ERROR_AUTH_FAILED) from exc
            raise SourceError(
                f"Diag API 请求失败: {detail or f'HTTP {exc.code}'}",
                code=_HTTP_ERROR_UNREACHABLE,
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceError(
                f"Diag API 请求失败: {exc.__class__.__name__}",
                code=_HTTP_ERROR_UNREACHABLE,
            ) from exc
        if not isinstance(payload, dict) or payload.get("code") not in (0, 200):
            detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
            is_auth_failure = isinstance(payload, dict) and payload.get("code") in (401, 403)
            code = _HTTP_ERROR_AUTH_FAILED if is_auth_failure else _HTTP_ERROR_UNREACHABLE
            raise SourceError(f"Diag API 拒绝查询: {detail}", code=code)
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
        missing = [
            name
            for name, configured in (
                ("base_url", bool(self.api.base_url)),
                ("internal_token_secret", bool(self.api.internal_token.secret)),
                ("internal_token_expire_seconds", bool(self.api.internal_token.expire_seconds)),
            )
            if not configured
        ]
        if missing:
            return {
                "ok": False,
                "status": "error",
                "error": _HTTP_ERROR_CONFIG_MISSING,
                "details": {"message": "Diag HTTP 配置不完整", "missing": missing},
            }
        try:
            self._get("/diag/redis-stream", {})
        except SourceError as exc:
            code = exc.code or _HTTP_ERROR_UNREACHABLE
            return {
                "ok": False,
                "status": "error",
                "error": code,
                "details": {"message": _HTTP_ERROR_MESSAGES.get(code, "Diag HTTP 检查失败")},
            }
        return {
            "ok": True,
            "status": "ok",
            "details": {
                "token_configured": bool(self.api.internal_token.secret),
                "token_expire_seconds": self.api.internal_token.expire_seconds,
                "cutover": "partial",
            },
        }


class HybridSources:
    def __init__(
        self,
        settings: Settings,
        *,
        allowed_devices: frozenset[str] | None = None,
    ) -> None:
        self.settings = settings
        self.tdengine = TDengineSource(settings, allowed_devices=allowed_devices)

    @property
    def http(self) -> HttpSources:
        try:
            return self._http
        except AttributeError:
            self._http = HttpSources(self.settings)
            return self._http

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

    def _http_doctor(self) -> dict[str, Any]:
        api = self.settings.http
        missing = [
            name
            for name, configured in (
                ("base_url", bool(api.base_url)),
                ("internal_token_secret", bool(api.internal_token.secret)),
                ("internal_token_expire_seconds", bool(api.internal_token.expire_seconds)),
            )
            if not configured
        ]
        if missing:
            return {
                "ok": False,
                "status": "error",
                "error": _HTTP_ERROR_CONFIG_MISSING,
                "details": {"message": "Diag HTTP 配置不完整", "missing": missing},
            }
        return self.http.doctor()

    def doctor(self) -> dict[str, Any]:
        result: dict[str, Any] = {"http": self._http_doctor()}
        try:
            details = self.tdengine.doctor()
        except SourceError as exc:
            result["tdengine"] = {"ok": False, "status": "error", "error": str(exc)}
        else:
            details["cutover"] = "partial"
            result["tdengine"] = {"ok": True, "status": "ok", "details": details}
        result["mysql"] = _deprecated_doctor()
        result["redis"] = _deprecated_doctor()
        return result


def _deprecated_doctor() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "deprecated",
        "details": {"message": "已收口：证据改由 /diag/* HTTP 接口返回，生产配置不应保留直连凭据"},
    }


class RedisSource:
    STREAMS = ("third.order.sync.queue", "third.order.sync.notify.queue")

    def __init__(
        self,
        settings: Settings,
        *,
        order_in_scope: Callable[[Mapping[Any, Any]], bool] | None = None,
    ) -> None:
        """白名单 Stream 只读检查。

        ``order_in_scope`` 为可选的归属验证谓词：只有谓词判定为 True 的消息
        才被计为该订单的匹配。无法验证租户/站点/订单归属时保持返回元数据而不
        返回原始消息正文；越权消息不计入匹配。
        """
        self.settings = settings
        self.order_in_scope = order_in_scope

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
                matches = sum(
                    _stream_fields_contain(fields, order_no) and _order_in_scope(self.order_in_scope, fields)
                    for _, fields in messages
                )
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

    def get_occupy_orders(
        self,
        order_id: str | None = None,
        order_no: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.mysql.get_occupy_orders(order_id, order_no, tenant_id)

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


class DeviceGate:
    """收集订单设备集合，供 TDengine 查询按权限范围校验。

    单次诊断运行内，先由受限 MySQL 订单查询把允许设备（``device_code`` /
    ``child_device_code``）写入本门；``TDengineSource`` 的 ``allowed_devices``
    在 seed 后生效。未 seed 任何设备时返回空集合，TDengine 查询保持拒绝
    （fail closed），不会退化为无范围直连。
    """

    def __init__(self) -> None:
        self._devices: set[str] = set()

    def seed_from_orders(self, orders: list[dict[str, Any]]) -> None:
        for order in orders:
            for key in ("device_code", "child_device_code"):
                code = str(order.get(key) or "").strip()
                if code:
                    self._devices.add(code)

    def allowed_devices(self) -> frozenset[str]:
        return frozenset(self._devices)


class ScopedSources:
    """受限直连诊断源：MySQL/TDengine/Redis 全部受同一 ``QueryScope`` 约束。

    ``device_gate`` 由订单元数据 seed，TDengine 只允许该设备集合内的设备；
    ``order_in_scope`` 由 ``make_redis_order_in_scope`` 构造，Redis 只统计租户
    归属可验证的消息。任一步骤缺失时 fail closed，不退回无范围直连。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        mysql: MySQLSource,
        tdengine: TDengineSource,
        redis: RedisSource,
        device_gate: DeviceGate | None = None,
    ) -> None:
        self.settings = settings
        self.mysql = mysql
        self.tdengine = tdengine
        self.redis = redis
        self.device_gate = device_gate

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        orders = self.mysql.get_orders(order_no, tenant_id)
        if self.device_gate is not None:
            self.device_gate.seed_from_orders(orders)
            self.tdengine.allowed_devices = self.device_gate.allowed_devices()
        return orders

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.mysql.get_fee_template_record(order_no, tenant_id)

    def get_occupy_orders(
        self,
        order_id: str | None = None,
        order_no: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.mysql.get_occupy_orders(order_id, order_no, tenant_id)

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

    def get_occupy_orders(
        self,
        order_id: str | None = None,
        order_no: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        has_order_id = bool(order_id and order_id.strip())
        has_order_no = bool(order_no and order_no.strip())
        if has_order_id == has_order_no:
            raise ValueError("占位费订单查询必须且只能提供 order_id 或 order_no 之一")
        records = [
            row
            for row in self.payload.get("occupy_orders", [])
            if (has_order_id and row.get("orderId") == order_id)
            or (has_order_no and row.get("order_no") == order_no)
        ]
        if tenant_id:
            records = [row for row in records if row.get("tenant_id") == tenant_id]
        return copy.deepcopy(records[:OCCUPY_ORDER_LIMIT])

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
def live_sources(
    settings: Settings,
    *,
    allowed_devices: frozenset[str] | None = None,
) -> Iterator[HybridSources]:
    """Return the current partial-cutover default source set.

    ``allowed_devices`` 非空时，TDengine 查询只允许该设备集合内的设备。
    """
    with _ssh_tunnel(settings, include_direct_backends=False) as effective:
        yield HybridSources(effective, allowed_devices=allowed_devices)


@contextlib.contextmanager
def scoped_live_sources(
    settings: Settings,
    *,
    scope: QueryScope,
) -> Iterator[ScopedSources]:
    """Return a fully scope-constrained direct source set for one run.

    MySQL 按 ``scope`` 下推租户/站点/用户；TDengine 只允许订单元数据里的设备
    （经 ``DeviceGate`` seed，未 seed 时拒绝）；Redis 只统计租户归属可验证的
    消息。SSH 隧道按直连回退路径建立三条转发（与 ``direct_sources`` 一致）。
    """
    device_gate = DeviceGate()
    with _ssh_tunnel(settings, include_direct_backends=True) as effective:
        yield ScopedSources(
            effective,
            mysql=MySQLSource(effective, scope=scope),
            tdengine=TDengineSource(effective, allowed_devices=frozenset()),
            redis=RedisSource(
                effective,
                order_in_scope=make_redis_order_in_scope(scope),
            ),
            device_gate=device_gate,
        )


@contextlib.contextmanager
def _ssh_tunnel(
    settings: Settings,
    *,
    include_direct_backends: bool = True,
) -> Iterator[Settings]:
    """Open only the SSH forwards required by the selected source set.

    The Phase 3a production path uses ``HybridSources`` and only needs the
    TDengine forward, so it must not request MySQL / Redis forwards that the
    tightened SSH ``permitopen`` policy no longer allows. The explicit
    rollback path keeps all three direct backends available.
    """
    if not settings.ssh.enabled:
        yield settings
        return

    settings.ssh.validate()
    ssh = settings.ssh
    if include_direct_backends:
        forwards = [
            ("mysql", ssh.mysql_host, ssh.mysql_port),
            ("tdengine", ssh.tdengine_host, ssh.tdengine_port),
            ("redis", ssh.redis_host, ssh.redis_port),
        ]
    else:
        forwards = [("tdengine", ssh.tdengine_host, ssh.tdengine_port)]

    local_ports = [_available_port() for _ in forwards]
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
    ]
    for (_name, remote_host, remote_port), local_port in zip(forwards, local_ports, strict=True):
        command.extend(["-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"])
    command.append(f"{ssh.user}@{ssh.host}")

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for port in local_ports:
            _wait_for_port(port, process, settings.safety.query_timeout_seconds)
        effective = copy.deepcopy(settings)
        for (name, _remote_host, _remote_port), local_port in zip(forwards, local_ports, strict=True):
            if name == "mysql":
                effective.mysql = replace(effective.mysql, host="127.0.0.1", port=local_port)
            elif name == "tdengine":
                effective.tdengine = replace(effective.tdengine, url=f"http://127.0.0.1:{local_port}")
            elif name == "redis":
                effective.redis = replace(effective.redis, host="127.0.0.1", port=local_port)
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


def _order_in_scope(
    predicate: Callable[[Mapping[Any, Any]], bool] | None,
    fields: Mapping[Any, Any],
) -> bool:
    """归属验证：无谓词时视为在范围内；有谓词时必须返回 True 才匹配。"""
    return predicate is None or predicate(fields)


def make_redis_order_in_scope(
    scope: QueryScope,
) -> Callable[[Mapping[Any, Any]], bool]:
    """从 ``QueryScope`` 构造 Redis 归属验证谓词。

    消息字段必须包含匹配 ``scope.tenant_id`` 的租户字段（``tenantId`` /
    ``tenant_id``，支持 str/bytes），才被计为范围内命中；无法从消息验证租户
    归属时不返回原始正文、不计数。站点/用户范围不直接用于 Redis 过滤——订单
    若已通过 MySQL scope 约束，则其同步消息即可被安全关联。
    """

    expected = scope.tenant_id

    def predicate(fields: Mapping[Any, Any]) -> bool:
        for key, value in fields.items():
            text_key = _decode_text(key)
            if text_key not in {"tenantId", "tenant_id"}:
                continue
            text_value = _decode_text(value)
            if text_value and text_value == expected:
                return True
        return False

    return predicate
