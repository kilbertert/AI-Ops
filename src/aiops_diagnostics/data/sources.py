from __future__ import annotations

import contextlib
import copy
import json
import re
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from aiops_diagnostics.common.bounded_http import (
    TEXT_PLAIN_CONTENT_TYPE,
    ErrorMapping,
    RequestSpec,
    basic_auth_header,
    join_url,
    parse_raw_envelope,
    request_json,
)
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.identity.order_visibility import (
    TenantVisibility,
    caller_visibility,
    normalize_tenant,
    scope_where_sql,
    visible_orders,
)
from aiops_diagnostics.identity.query_scope import (
    SCOPE_ERROR_SCOPE_TOO_LARGE,
    QueryScope,
    SiteScopeMapper,
)
from aiops_diagnostics.identity.scope_context import ScopeError
from aiops_diagnostics.operator.ops_analysis import ABNORMAL_STATUS, NORMAL_STATUS

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SourceError(RuntimeError):
    """A bounded read-only data-source operation failed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


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

#: 站点归属解析（ch_site）单次返回行数上限。
SITE_SCOPE_MAX_ROWS = 1000

#: 故障码排行取前几名：与公司 ``getErrRank`` 的 ``limit 7`` 一致（#698）。
FAULT_CODE_RANK_LIMIT = 7


#: pymysql 的参数占位符（paramstyle=format）；共享规则按此渲染租户谓词。
_SQL_PARAM = "%s"

#: ``ch_order_info.user_id`` 取这个值时，订单的**消费者**是 ``consumer_id`` 那一列
#: （工单场景）。公司侧同一口径：``ChSendOrderMsgServiceImpl.getUserId``
#: ``userId == "-1" ? consumerId : userId``，``ChargingServiceImpl`` 建工单订单时
#: 写的就是 ``-1``。41 生产库实测 771 行，其中 347 行带 ``consumer_id``。
#: 只认 ``-1``：空白或 NULL 是**没有人**认领的行（渠道/OCPI 单据），不是本人。
WORK_ORDER_USER = "-1"

#: 订单表上「认领者」的第二列。只有 ``ch_order_info`` 有它；占位单、设备表没有，
#: 所以它是 ``_scope_where`` 的开关而不是全局行为（见 ``_scope_where``）。
ORDER_CLAIM_COLUMN = "consumer_id"


def _placeholders(count: int) -> str:
    return ", ".join(_SQL_PARAM for _ in range(count))


def _caller_visible(rows: Sequence[Mapping[str, Any]], tenant_id: str | None) -> list[Mapping[str, Any]]:
    """Apply the shared row-level rule to the rows one source holds in memory.

    Two different inputs, two different meanings, and the difference is the
    whole point:

    - a caller that names *no* tenant at all imposes no constraint and sees
      every row — the discovery mode an unbound device registration uses, and
      the same unrestricted state the ``DEVICE`` profile spells ``allowed=None``;
    - a caller that names one is judged by the shared rule, which normalizes the
      row tenant before comparing: a padded identifier matches instead of
      silently missing, and a tenant that cannot be normalized sees *nothing*
      rather than binding a blank value to match.

    "Named but unusable" must not collapse into "not named": a blank or
    whitespace tenant that fell through to the unrestricted branch would show
    every row, which is the fail-open direction in the one filter this module
    converges. It goes to :func:`caller_visibility` instead, whose empty scope
    renders as ``1=0`` in SQL — the same conclusion the row-level rule reaches,
    because both are renderings of one definition.
    """
    if tenant_id is None:
        return list(rows)
    return list(visible_orders(rows, caller_visibility(tenant_id)).rows)


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

    def _tenant_visibility(self) -> TenantVisibility:
        """把冻结 scope 表达成共享租户可见性规则的输入（caller profile）。"""
        scope = self.scope
        assert scope is not None
        return caller_visibility(scope.tenant_id)

    def _scope_where(
        self,
        *,
        site_column: str | None = "site_id",
        user_column: str | None = "user_id",
        claim_column: str | None = None,
    ) -> tuple[str, list[Any]]:
        """构造 scope 下推的 WHERE 片段（不含前导 AND，调用方自行拼接）。

        租户谓词由共享租户可见性规则渲染（参数绑定），与行级过滤是同一处定义
        的两种渲染；站点/用户列名来自固定表结构常量，不是客户端输入，值一律
        参数绑定。

        ``claim_column`` 是「这张表的认领者还有第二列」的渲染开关，只有订单表
        打开它（见 :data:`ORDER_CLAIM_COLUMN`）：自带该列的表按同一规则多认一个
        认领者，没有该列的表（占位单、设备）保持单列语义不变。
        """
        scope = self.scope
        if scope is None:
            return "", []
        tenant_where, params = scope_where_sql("tenant_id", self._tenant_visibility(), placeholder=_SQL_PARAM)
        fragments: list[str] = [tenant_where]
        if scope.site_ids is not None and site_column:
            fragments.append(f"{site_column} IN ({_placeholders(len(scope.site_ids))})")
            params.extend(scope.site_ids)
        if scope.user_id and user_column:
            if claim_column:
                # 认领者 = `user_id`，但工单订单把它写成哨兵值、真人在 `consumer_id`。
                # 写成一个片段（而不是 `a OR (b AND c)`）：片段以 ` AND ` 连接、调用方
                # 自行拼接，带 ` AND ` 的片段会让每一个 SQL 镜像与解析器都要重新学一遍
                # 括号；`CASE` 版本仍是「一个片段、两个参数」。
                fragments.append(
                    f"(CASE WHEN {user_column}={_SQL_PARAM} THEN {claim_column} ELSE {user_column} END"
                    f"={_SQL_PARAM})"
                )
                params.extend([WORK_ORDER_USER, scope.user_id])
            else:
                fragments.append(f"{user_column}={_SQL_PARAM}")
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
                server_public_key=True,
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
        # 订单表打开第二列认领者：``self`` 范围下「本人下的单」与「本人是工单消费者的单」
        # 是同一件事，只认 ``user_id`` 会让后者的主人自己被判成「这个订单不属于当前账号」。
        scope_where, scope_params = self._scope_where(claim_column=ORDER_CLAIM_COLUMN)
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

    def list_orders(self, *, page: int, page_size: int) -> list[dict[str, Any]]:
        """只输出选单需要的字段；无冻结范围时不开放列表。"""
        if self.scope is None:
            raise SourceError("order list requires a query scope")
        if not 1 <= page <= 1000 or not 1 <= page_size <= 50:
            raise SourceError("order list pagination is out of bounds")
        if self._scope_blocked():
            return []
        where, params = self._scope_where(claim_column=ORDER_CLAIM_COLUMN)
        sql = (
            f"SELECT order_no, status, site_id, created_time FROM `{self.database}`.`ch_order_info` "
            f"WHERE {where} ORDER BY created_time DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, [*params, page_size + 1, (page - 1) * page_size])
            return [_normalize_row(row) for row in cursor.fetchall()]

    def aggregate_order_statuses(
        self,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any] | None:
        """按站点集合 + 时间窗统计订单状态（#697 的运维分析第一刀）。

        这是本仓**第一条聚合 SQL**，所以它的形状值得写下来：

        * **交集在本函数内计算，不交给调用方。** 租户谓词永远下推（``_scope_where``
          渲染的那一段），而请求的站点集合先与调用者可见站点取交集，**取完再**成为
          ``site_id IN (...)``。把「调用方已经窄过了」当前提是本条最容易写错的地方：
          一旦有一个调用方传进未经收窄的集合，超出范围的站点就会被如实统计出来，
          而查询本身成功、数字本身"合理"。
        * **``None`` 与 ``()`` 是两件事**（``_bounded_sites`` 的同一套三值约定，本函数
          在 #721 才补上 —— 它是最早写的一条，另外三条卡同族函数一开始就有）。
          ``None`` = **调用方没要求收窄**，此时若调用者自己的可见范围本来就是
          「租户内不限站点」（``scope.site_ids is None``）就**不加站点条件**，租户谓词
          照下推；``()`` = **交集为空**，短路不发 SQL。把它俩一起读成「空」会让
          **范围最宽的账号——整租户账号——恒得空结果**，而那正是这个面为之存在的账号。
        * **``_scope_blocked()``（可见范围本身为空的短路口）同样不发 SQL。** 与 ``()``
          一样返回 ``None``，由上层说成 ``unavailable``（「没人查过的范围」不是「没有异常」）。
        * **时间窗是半开的** ``[window_start, window_end)``：闭开区间让相邻两窗既不复算
          也不漏算，而这一点写进对外契约的 ``definition``，不靠读代码才知道。
        * 时间列 ``created_time``、状态列 ``status``、站点列 ``site_id`` 都在
          ``ORDER_COLUMNS`` 里——列名来自固定表结构常量，不是客户端输入。

        异常结束的取值见 ``ops_analysis.ABNORMAL_STATUS``（其证据也写在那里）。

        只回四样东西：总数、异常结束数、正常结束数，以及异常最多的一台站点。
        **成因分类不在这里做**：它住在 ``rules.classify_stop_reason`` 里，是 Python
        不是列，按成因聚合需要把窗口内的行全拉回来——那会破掉「不下推就不查」这条房规
        （见 ``ops_analysis._secondary``）。
        """
        if self._scope_blocked():
            return None
        # The intersection happens HERE, not in the caller: this is the layer
        # that must not exceed the scope, so it cannot rely on the caller having
        # narrowed the set correctly.
        requested = self._bounded_sites(site_ids)
        if requested == ():
            return None
        scope_where, params = self._scope_where(site_column=None, user_column=None)
        fragments = [fragment for fragment in (scope_where,) if fragment]
        if requested is not None:
            fragments.append(f"site_id IN ({_placeholders(len(requested))})")
            params.extend(requested)
        fragments.append("created_time >= %s")
        params.append(window_start)
        fragments.append("created_time < %s")
        params.append(window_end)
        where = " AND ".join(fragments)
        hint = f"/*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */"
        # The two role codes are BOUND, not interpolated (#732). They come from the
        # authority module today and are integers, so the f-string form was not an
        # injection — but "a constant that will never be a string" is a property of
        # today's value, not of the seam, and `sources` is where the failure_rate
        # numerator is decided.
        #
        # ⚠️ **Order is the SQL's, not the statement's.** In this summary the two
        # status placeholders sit in the SELECT list, which is emitted BEFORE the
        # WHERE — so they bind first and the scope/window values follow. The
        # `top_sql` below has its placeholder AFTER the WHERE and therefore binds
        # last. Getting this backwards does not raise: it silently counts the wrong
        # rows (a tenant id bound as a status). Caught in the #735 review — and the
        # tests could not see it, because their cursor records parameters instead of
        # executing, so nothing ever compared a placeholder to its value.
        sql = (
            f"SELECT {hint} COUNT(*) AS total, "
            "SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS abnormal_ended, "
            "SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS normal_ended "
            f"FROM `{self.database}`.`ch_order_info` WHERE {where}"
        )
        top_sql = (
            f"SELECT {hint} site_id, COUNT(*) AS abnormal_ended "
            f"FROM `{self.database}`.`ch_order_info` "
            f"WHERE {where} AND status = %s "
            "GROUP BY site_id ORDER BY abnormal_ended DESC, site_id ASC LIMIT 1"
        )
        role_params = [ABNORMAL_STATUS, NORMAL_STATUS]  # SELECT-list order
        with self._cursor() as cursor:
            cursor.execute(sql, [*role_params, *params])
            row = cursor.fetchone() or {}
            cursor.execute(top_sql, [*params, ABNORMAL_STATUS])
            top = cursor.fetchone()
        return {
            "total": _count_or_zero(row.get("total")),
            "abnormal_ended": _count_or_zero(row.get("abnormal_ended")),
            "normal_ended": _count_or_zero(row.get("normal_ended")),
            "top_sites": ((str(top["site_id"]), _count_or_zero(top["abnormal_ended"])),) if top else (),
        }

    def aggregate_payment_split(
        self,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any] | None:
        """支付卡（#699）：窗内订单**按支付 / 退款状态**的四类计数。

        **这不是「支付失败率」，这是能算出来的那一部分。** 公司的订单表上只有结果态，
        没有「支付尝试失败」这个事实：``is_pay`` 是布尔（是否支付完成，见
        ``V1.0.93`` 对 occupy 表同名列的 ``comment '是否支付'``），``refund_status``
        是 0/1/2（未退款 / 全额 / 部分，见 ``V1.0.44__add_order.sql``）。一次被支付
        渠道拒绝、随后重试成功的支付**在这两列上不留痕迹**——它是「已支付」。因此
        本函数回的是四个**互斥且可复核**的桶，而不是一个带成因的比率：

        * ``paid``：``is_pay`` 为真；
        * ``paid_refunded``：已支付且 ``refund_status`` 非 0（全额或部分已退款）；
        * ``paid_net``：已支付且未退款；
        * ``unpaid``：``is_pay`` 不是真（含 NULL）。

        ``is_pay`` 可空，所以判据写成 ``IS TRUE`` 前的显式三态：NULL **计入未支付**，
        并在 ``definition`` 里写明。把它当"未知"丢掉会让两个桶的数量对不上总数，
        而读者无从知道差额来自哪里。

        接线（``gateway_runtime``）据此把卡的 ``failure_rate`` 判成 ``unavailable``
        并给 ``reason_code=NO_PAYMENT_FAILURE_SOURCE`` —— 诚实标缺口是本票的验收项，
        不是退让。
        """
        if self._scope_blocked():
            return None
        requested = self._bounded_sites(site_ids)
        if requested is not None and not requested:
            return None
        scope_where, params = self._scope_where(site_column=None, user_column=None)
        fragments = [fragment for fragment in (scope_where,) if fragment]
        if requested is not None:
            fragments.append(f"site_id IN ({_placeholders(len(requested))})")
            params.extend(requested)
        fragments.append("created_time >= %s")
        params.append(window_start)
        fragments.append("created_time < %s")
        params.append(window_end)
        where = " AND ".join(fragments)
        hint = f"/*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */"
        sql = (
            f"SELECT {hint} COUNT(*) AS total, "
            "SUM(CASE WHEN is_pay IS TRUE THEN 1 ELSE 0 END) AS paid, "
            "SUM(CASE WHEN is_pay IS TRUE AND refund_status IS NOT NULL "
            "AND refund_status <> 0 THEN 1 ELSE 0 END) AS paid_refunded, "
            "SUM(CASE WHEN is_pay IS TRUE AND (refund_status IS NULL OR refund_status = 0) "
            "THEN 1 ELSE 0 END) AS paid_net, "
            "SUM(CASE WHEN is_pay IS NOT TRUE THEN 1 ELSE 0 END) AS unpaid "
            f"FROM `{self.database}`.`ch_order_info` WHERE {where}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, list(params))
            row = cursor.fetchone() or {}
        return {
            "total": _count_or_zero(row.get("total")),
            "paid": _count_or_zero(row.get("paid")),
            "paid_refunded": _count_or_zero(row.get("paid_refunded")),
            "paid_net": _count_or_zero(row.get("paid_net")),
            "unpaid": _count_or_zero(row.get("unpaid")),
        }

    def count_devices(self, site_ids: tuple[str, ...] | None) -> int | None:
        """范围内的设备总数——两张设备卡的**分母**（#698）。

        没有分母就没有比率：一张只说「12 台离线」的卡无法回答「这算多吗」。
        ``site_ids`` 为 ``None`` 表示用调用者范围内的全部站点；交集为空返回 ``None``
        （与别处同形：没查过的范围不是「0 台设备」）。
        """
        if self._scope_blocked():
            return None
        requested = self._bounded_sites(site_ids)
        if requested is not None and not requested:
            return None
        scope_where, params = self._scope_where(site_column=None, user_column=None)
        fragments = [fragment for fragment in (scope_where,) if fragment]
        if requested is not None:
            fragments.append(f"site_id IN ({_placeholders(len(requested))})")
            params.extend(requested)
        where = " AND ".join(fragments) if fragments else "1=1"
        hint = f"/*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */"
        sql = (
            f"SELECT {hint} COUNT(DISTINCT id) AS devices_total "
            f"FROM `{self.database}`.`iot_charging_device` WHERE {where}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, list(params))
            row = cursor.fetchone() or {}
        return _count_or_zero(row.get("devices_total"))

    def aggregate_offline_incidents(
        self,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any] | None:
        """离线桩卡（#698）：台数、时长量级、仍在离线中的条数。

        表与列的证据：``iot_device_offline_monitor`` 是公司自己的离线监控表，
        建表语句在 ``cloud-charging-pile-core/src/main/resources/db/migration/
        V1.0.110__iot_offline_monitor.sql``（列 ``device_id`` / ``site_id`` /
        ``offline_time`` / ``recover_time`` / ``tenant_id``），加上
        ``V1.1.37__add_vol.sql``（``status``）与 ``V1.1.92__add_field_device_type.sql``
        （``operation_type``）。**列名是抄 DDL 的，不是猜的**——
        ``offline_time`` 与 ``recover_time`` 都可空，所以下面两个 CASE 都要处理 NULL。

        ⚠️ **该表的 ``tenant_id`` 可空**（DDL 如此），因此租户谓词会漏掉
        ``tenant_id IS NULL`` 的行。本函数**仍然下推租户**：范围边界不能因为某张表
        恰好写得不严就放宽；漏掉的行是**数据缺口**，其量级未在 41 上测过，
        如实记在这里而不是靠放宽范围去"补上"。

        **时长只累计已恢复的那些，并被窗口末端截断。** 仍在离线中的记录其结束时间
        在查询时刻还不存在，用「现在」去算会让同一个窗口两次查询给出不同的数字——
        那正是这张卡承诺要避免的事。因此未恢复的**只计数、不计时长**，并作为
        ``open_incidents`` 单独交付：读的人据此知道时长是**下界**而不是全貌。
        """
        if self._scope_blocked():
            return None
        requested = self._bounded_sites(site_ids)
        if requested is not None and not requested:
            return None
        scope_where, params = self._scope_where(site_column=None, user_column=None)
        fragments = [fragment for fragment in (scope_where,) if fragment]
        if requested is not None:
            fragments.append(f"site_id IN ({_placeholders(len(requested))})")
            params.extend(requested)
        fragments.append("offline_time >= %s")
        params.append(window_start)
        fragments.append("offline_time < %s")
        params.append(window_end)
        where = " AND ".join(fragments)
        hint = f"/*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */"
        # The clamp is a SELECT-list parameter, so it precedes every WHERE
        # parameter: the SQL is built top to bottom and the order must follow it.
        sql = (
            f"SELECT {hint} COUNT(*) AS incidents, "
            "COUNT(DISTINCT device_id) AS offline_devices, "
            "SUM(CASE WHEN recover_time IS NULL THEN 1 ELSE 0 END) AS open_incidents, "
            "SUM(CASE WHEN recover_time IS NULL THEN 0 ELSE GREATEST("
            "TIMESTAMPDIFF(MINUTE, offline_time, LEAST(recover_time, %s)), 0) END) AS offline_minutes "
            f"FROM `{self.database}`.`iot_device_offline_monitor` WHERE {where}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, [window_end, *params])
            row = cursor.fetchone() or {}
        return {
            "incidents": _count_or_zero(row.get("incidents")),
            "offline_devices": _count_or_zero(row.get("offline_devices")),
            "open_incidents": _count_or_zero(row.get("open_incidents")),
            "offline_minutes": _count_or_zero(row.get("offline_minutes")),
        }

    def aggregate_fault_codes(
        self,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any] | None:
        """故障码排行卡（#698）。

        公式参考公司自己的 ``IotDeviceFailureRecordsMapper.getErrRank``
        （``group by error_code order by errCount desc limit 7``）——**只参考公式**：
        那一条**既没有租户条件也没有时间窗**，直接用会把全库排行当成某个租户某个区间
        的排行。执行落在这里的只读 SQL 上，范围与时间窗都由 ``QueryScope`` 收。

        表与列的证据：``V1.0.89__iot_failure_recore.sql``（表与 ``device_id`` /
        ``error_code`` / ``error_reason`` / ``tenant_id``）、
        ``V1.0.109__iot_failure_add.sql``（``site_id``）、
        ``V1.0.104__iot_device_failure.sql``（``fault_time``）、
        ``V1.1.28__add_colum_failure.sql``（``order_no``）。``fault_time`` 可空，
        而窗口条件会把 NULL 排除——这是**有意的**：没有故障时间的记录无法归入任何
        一个区间，硬塞进某一个会让这张卡的时间语义失真。
        """
        if self._scope_blocked():
            return None
        requested = self._bounded_sites(site_ids)
        if requested is not None and not requested:
            return None
        scope_where, params = self._scope_where(site_column=None, user_column=None)
        fragments = [fragment for fragment in (scope_where,) if fragment]
        if requested is not None:
            fragments.append(f"site_id IN ({_placeholders(len(requested))})")
            params.extend(requested)
        fragments.append("fault_time >= %s")
        params.append(window_start)
        fragments.append("fault_time < %s")
        params.append(window_end)
        where = " AND ".join(fragments)
        hint = f"/*+ MAX_EXECUTION_TIME({int(self.settings.safety.mysql_max_execution_ms)}) */"
        sql = (
            f"SELECT {hint} COUNT(*) AS total_faults, "
            "COUNT(DISTINCT device_id) AS faulty_devices "
            f"FROM `{self.database}`.`iot_device_failure_records` WHERE {where}"
        )
        rank_sql = (
            f"SELECT {hint} error_code, COUNT(*) AS fault_count "
            f"FROM `{self.database}`.`iot_device_failure_records` WHERE {where} "
            "AND error_code IS NOT NULL GROUP BY error_code "
            f"ORDER BY fault_count DESC, error_code ASC LIMIT {FAULT_CODE_RANK_LIMIT}"
        )
        with self._cursor() as cursor:
            cursor.execute(sql, list(params))
            row = cursor.fetchone() or {}
            cursor.execute(rank_sql, list(params))
            ranked = cursor.fetchall() or []
        return {
            "total_faults": _count_or_zero(row.get("total_faults")),
            "faulty_devices": _count_or_zero(row.get("faulty_devices")),
            "top_codes": tuple(
                (str(item["error_code"]), _count_or_zero(item["fault_count"])) for item in ranked
            ),
        }

    def _bounded_sites(self, site_ids: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """本次查询**实际**要筛的站点集合，三种结果各不相同。

        * ``None`` —— **不加站点条件**：调用方没指定，而它自己的可见范围本来就是
          「租户内不限站点」（``scope.site_ids is None``）。此时租户谓词仍由
          ``_scope_where`` 下推。
        * ``()`` —— **筛出来是空的**：请求的集合与可见站点没有交集。调用方据此短路，
          **不发 SQL**；把它当成「不收窄」会让一次越界的请求变成一次全租户扫描。
        * 非空元组 —— 请求集合与可见站点的交集（去重后）。

        交集放在本层而不是调用方，理由与 ``aggregate_order_statuses`` 相同：这一层
        才是「不得超出范围」的守卫边界，把「调用方已经窄过了」当前提意味着任何一次
        调用方疏忽都会如实统计出无权看到的站点，而查询成功、数字合理。
        """
        scope_sites = None if self.scope is None else self.scope.site_ids
        if site_ids is None:
            # No request-level narrowing: the caller's own range decides, and a
            # range of `None` needs no site predicate at all.
            return None if scope_sites is None else tuple(scope_sites)
        requested = tuple(dict.fromkeys(site_ids))
        if scope_sites is not None:
            allowed = set(scope_sites)
            requested = tuple(site for site in requested if site in allowed)
        return requested

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
        # 多取一行：LIMIT 恰好等于上限时无法区分「正好这么多」与「被截断」，而上游
        # 把截断当完整集合用（MAX_SCOPE_IDS 与 SITE_SCOPE_MAX_ROWS 恰好相等）会
        # 静默漏掉超出的站点。因此以「是否取到第 MAX+1 行」判定超界并 fail closed。
        sql = (
            f"SELECT id FROM `{self.database}`.`ch_site` "
            f"WHERE tenant_id=%s AND {column} IN ({_placeholders(len(values))}) "
            f"ORDER BY id LIMIT {SITE_SCOPE_MAX_ROWS + 1}"
        )
        params: list[Any] = [tenant_id, *values]
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        ids = tuple(str(row["id"]) for row in rows if row.get("id") is not None)
        if len(ids) > SITE_SCOPE_MAX_ROWS:
            raise ScopeError(f"站点归属数量超过上限 {SITE_SCOPE_MAX_ROWS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)
        return ids


# charging-gun_property 查询列的唯一权威清单：
# TDengineSource.get_gun_samples 的 SELECT、
# tdengine_proxy 只读白名单全部从这里派生，防止三处各自维护漂移。
GUN_COLUMN_NAMES: tuple[str, ...] = (
    "txSerialNo",
    "status",
    "isReturn",
    "isInsert",
    "outputVoltage",
    "outputCurrent",
    "power",
    "chargingTime",
    "chargingElectricityQuantity",
    "soc",
    "temperature",
    "batteryMaxTemperature",
    "batteryMinTemperature",
    "errorCode",
    "errorReason",
    "meterNow",
)


def _gun_columns_sql() -> str:
    """与现网 SQL 逐字节一致的 SELECT 列串：仅含大写的 camelCase 列加反引号
    （TDengine 大小写敏感标识符），全小写列裸写。tdengine_proxy 白名单正则
    与 get_gun_samples 共用本函数，代理是独立部署的 root 服务——SQL 形状
    漂移会在滚动窗口内被生产代理拒绝，所以这里必须保持字节级稳定。"""

    def quote(column: str) -> str:
        return f"`{column}`" if column != column.lower() else column

    return "_ts, " + ", ".join(quote(column) for column in GUN_COLUMN_NAMES)


GUN_COLUMNS_SQL = _gun_columns_sql()


class TDengineSource:
    """TDengine 只读查询；可选携带单次运行冻结的允许设备集合。

    携带 ``allowed_devices``（非空）时，每个查询的 ``device`` 必须在集合内，
    否则拒绝且不发起 TDengine 请求。``None`` 表示由上游订单 scope 已约束
    设备（订单本身来自受信任的 MySQL scope 查询），但仍走固定的超表/字段/
    时间窗/LIMIT 构造，不接受客户端透传表名、字段或 SQL。
    """

    # get_gun_samples 的完整 SELECT 列表（41 环境曾缺 batteryMinTemperature，
    # 与只读代理的列白名单保持一致）。
    # 清单唯一来源是模块级 GUN_COLUMN_NAMES；tdengine_proxy 白名单也从它派生。
    GUN_COLUMNS: tuple[str, ...] = GUN_COLUMN_NAMES

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
        payload = request_json(
            RequestSpec(
                url=join_url(cfg.url, f"/rest/sql/{self.database}"),
                method="POST",
                headers={
                    "Authorization": basic_auth_header(cfg.user, cfg.password),
                    "Content-Type": TEXT_PLAIN_CONTENT_TYPE,
                },
                body=sql.encode("utf-8"),
                timeout=self.settings.safety.query_timeout_seconds,
            ),
            mapping=ErrorMapping(
                auth_rejected=lambda f: SourceError(f"TDengine 查询失败: {f.detail}"),
                http_error=lambda f: SourceError(f"TDengine 查询失败: HTTP {f.status}"),
                unavailable=lambda f: SourceError(f"TDengine 查询失败: {f.detail}"),
                invalid_body=lambda f: SourceError(f"TDengine 查询失败: {f.detail}"),
                invalid_envelope=lambda f: SourceError(f"TDengine 查询失败: {f.detail}"),
            ),
            envelope=parse_raw_envelope,
        )
        if not isinstance(payload, dict):
            raise SourceError("TDengine 查询失败: invalid response")
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
            f"SELECT {GUN_COLUMNS_SQL} "
            "FROM `charging-gun_property` "
            f"WHERE device='{device_literal}' AND _ts>='{_format_time(start_time)}' "
            f"AND _ts<='{_format_time(end_time)}'{tx_filter} ORDER BY _ts ASC LIMIT {limit}"
        )
        return self._query(sql)


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
    """受限直连诊断源：MySQL/TDengine 受同一 ``QueryScope`` 约束。

    ``device_gate`` 由订单元数据 seed，TDengine 只允许该设备集合内的设备；
    未授权设备拒绝读取，不退回无范围直连。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        mysql: MySQLSource,
        tdengine: TDengineSource,
        device_gate: DeviceGate | None = None,
    ) -> None:
        self.settings = settings
        self.mysql = mysql
        self.tdengine = tdengine
        self.device_gate = device_gate

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        orders = self.mysql.get_orders(order_no, tenant_id)
        if self.device_gate is not None:
            self.device_gate.seed_from_orders(orders)
            self.tdengine.allowed_devices = self.device_gate.allowed_devices()
        return orders

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


class FixtureSources:
    def __init__(self, path: Path) -> None:
        self.payload = json.loads(path.read_text(encoding="utf-8"))

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        orders = [row for row in self.payload.get("orders", []) if row.get("order_no") == order_no]
        # Tenant visibility is one shared rule (#325); this source no longer
        # carries its own comparison.
        return copy.deepcopy(_caller_visible(orders, tenant_id))

    def get_device(
        self,
        device_id: str | None,
        device_code: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        for device in self.payload.get("devices", []):
            if not (
                (device_id and device.get("id") == device_id)
                or (device_code and device.get("device_code") == device_code)
            ):
                continue
            # A device row that states no tenant is not hidden by a tenant filter:
            # the device catalog is looked up by the order's tenant, and the
            # ``/diag/device`` contract binds a tenant only when one is supplied.
            # This is not the order-visibility rule — a row that *does* state a
            # tenant is judged by the shared rule like every other row.
            if not normalize_tenant(device.get("tenant_id")) or _caller_visible([device], tenant_id):
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
    with _ssh_tunnel(settings, forwards=("mysql", "tdengine")) as effective:
        yield ScopedSources(
            effective,
            mysql=MySQLSource(effective, scope=scope),
            tdengine=TDengineSource(effective, allowed_devices=frozenset()),
            device_gate=device_gate,
        )


@contextlib.contextmanager
def scoped_mysql_source(settings: Settings, scope: QueryScope) -> Iterator[MySQLSource]:
    with _ssh_tunnel(settings, forwards=("mysql",)) as effective:
        yield MySQLSource(effective, scope=scope)


@contextlib.contextmanager
def mysql_site_mapper(settings: Settings) -> Iterator[SiteScopeMapper]:
    """Return the charging-library site-ownership mapper for one operation.

    站点归属映射（``ch_site.shop_id → ch_site.id``）在授权范围解析里用（运营商站点
    集合，#426）。它必须与受限直连在同一条跳板隧道内构造 ``MySQLSource``：在隧道外
    构造会绕开 ``_ssh_tunnel`` 的端口转发，直连一个 SSH 部署下不可达的数据库地址。

    只请求 ``mysql`` 一条转发：这里只需要 MySQL。按 ``permitopen`` 收紧后的 SSH
    策略多要 TDengine/Redis 转发，会让 ``ExitOnForwardFailure=yes`` 直接杀掉 ssh
    进程——于是整个管家端授权链因为一个与它无关的拒绝而 fail closed。

    ``MySQLSource`` 不带 ``scope``：归属映射自己带租户条件，与单次运行冻结的
    查询范围无关。
    """
    with _ssh_tunnel(settings, forwards=("mysql",)) as effective:
        yield MySQLSource(effective)


@contextlib.contextmanager
def _ssh_tunnel(
    settings: Settings,
    *,
    forwards: tuple[str, ...] = ("mysql", "tdengine", "redis"),
) -> Iterator[Settings]:
    """Open only the SSH forwards required by the selected source set.

    The Phase 3a production path uses ``HybridSources`` and only needs the
    TDengine forward, so it must not request MySQL / Redis forwards that the
    tightened SSH ``permitopen`` policy no longer allows. The explicit
    rollback path keeps all three direct backends available. An authorization
    helper asks for the one forward it really uses — with
    ``ExitOnForwardFailure=yes`` a forward the policy rejects kills the ssh
    process and takes the whole operation down with it.
    """
    if not settings.ssh.enabled:
        yield settings
        return

    settings.ssh.validate()
    ssh = settings.ssh
    remote_by_name = {
        "mysql": (ssh.mysql_host, ssh.mysql_port),
        "tdengine": (ssh.tdengine_host, ssh.tdengine_port),
        "redis": (ssh.redis_host, ssh.redis_port),
    }
    selected = [(name, *remote_by_name[name]) for name in forwards]

    local_ports = [_available_port() for _ in selected]
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
    for (_name, remote_host, remote_port), local_port in zip(selected, local_ports, strict=True):
        command.extend(["-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"])
    command.append(f"{ssh.user}@{ssh.host}")

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for port in local_ports:
            _wait_for_port(port, process, settings.safety.query_timeout_seconds)
        effective = copy.deepcopy(settings)
        for (name, _remote_host, _remote_port), local_port in zip(selected, local_ports, strict=True):
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


_ROLE_ACCOUNT = re.compile(r"`(?P<name>[A-Za-z0-9_.:-]{1,128})`@`(?P<host>[A-Za-z0-9_.:%*-]{1,255})`")


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _count_or_zero(value: Any) -> int:
    """把聚合结果里可能为 NULL 的计数收敛成整数。

    ``SUM(CASE ...)`` 在**没有行**时返回 NULL 而不是 0，而「0 行」与「SQL 说不清」
    在契约里是两个结论。这里把 NULL 判成 0 是安全的：行数由同一次查询的 ``COUNT(*)``
    给出，因此「NULL 的 SUM」只可能意味着那一类计数确实是 0。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


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
