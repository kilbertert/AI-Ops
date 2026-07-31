from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.models import DiagnosticReport, DiagnosticRequest, Evidence, Intent, Severity
from aiops_diagnostics.rules import classify_stop_reason, is_server_billing, status_label
from aiops_diagnostics.sources import DiagnosticSources, SourceError

AMOUNT_TOLERANCE = Decimal("0.02")
ENERGY_TOLERANCE = Decimal("0.001")


class DiagnosticEngine:
    def __init__(self, sources: DiagnosticSources, safety: SafetySettings) -> None:
        self.sources = sources
        self.safety = safety

    def diagnose(self, request: DiagnosticRequest) -> DiagnosticReport:
        report = DiagnosticReport(request=request, summary="诊断未完成")
        orders = self._load_orders(report)
        if orders is None:
            return report
        if not orders:
            report.summary = "未查询到该订单，无法继续关联时序和通讯数据"
            report.classifications.append("order_not_found")
            report.next_steps.append("确认订单号、环境和租户范围是否正确")
            return report
        if len(orders) > 1:
            report.summary = "同一订单号匹配到多条记录，必须指定 tenant_id 后才能继续"
            report.classifications.append("ambiguous_order")
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="订单号不唯一",
                    observation=f"命中 {len(orders)} 条记录",
                    severity=Severity.CRITICAL,
                    facts={"tenant_count": len({item.get("tenant_id") for item in orders})},
                )
            )
            report.next_steps.append("使用 --tenant-id 限定租户，避免跨租户误诊")
            return report

        order = orders[0]
        report.order_facts = _public_order_facts(order)
        self._analyze_status(order, report)
        tx_data, tx_serial_no = self._analyze_transaction(order, report)
        self._inspect_fee_snapshot(order, report)
        self._inspect_device(order, report)
        self._inspect_tdengine(order, tx_serial_no, report)
        self._inspect_redis(order, report)
        self._finalize(order, tx_data, report)
        return report

    def _load_orders(self, report: DiagnosticReport) -> list[dict[str, Any]] | None:
        try:
            orders = self.sources.get_orders(report.request.order_no, report.request.tenant_id)
            report.queried_sources.append("mysql:ch_order_info")
            return orders
        except SourceError as exc:
            report.summary = "MySQL 订单查询失败"
            report.classifications.append("source_unavailable")
            report.limitations.append(str(exc))
            report.next_steps.append("检查只读账号、网络和数据库连接配置")
            return None

    def _analyze_status(self, order: dict[str, Any], report: DiagnosticReport) -> None:
        status = _to_int(order.get("status"))
        label = status_label(status)
        severity = Severity.INFO
        observation = f"订单状态为 {label}"
        if status == 2:
            severity = Severity.CRITICAL
            report.classifications.append("uncontrollable_exception")
            observation += "，源码定义为断网、离线等不可控异常"
        elif status == 3:
            severity = Severity.WARNING
            report.classifications.append("exception_already_handled")
            observation += "，后续交易数据会被业务代码拒绝重复处理"
        elif status == 5:
            severity = Severity.WARNING
            report.classifications.append("controlled_abnormal_end")
            observation += "，设备已上报结束事件，但停止原因被判定为非正常"
        elif status == 0:
            report.classifications.append("charging")
        elif status == 1:
            report.classifications.append("finished")
        else:
            severity = Severity.WARNING
            report.classifications.append("unknown_order_status")
        report.evidence.append(
            Evidence(
                source="mysql",
                title="订单业务状态",
                observation=observation,
                severity=severity,
                facts={"status": status, "status_label": label},
            )
        )

        stop = classify_stop_reason(
            str(order.get("device_protocol") or ""),
            order.get("stopped_reason_code"),
            str(order.get("stopped_reason_content") or ""),
        )
        stop_severity = Severity.WARNING if stop.abnormal is not False else Severity.INFO
        report.evidence.append(
            Evidence(
                source="mysql",
                title="停止原因",
                observation=stop.description,
                severity=stop_severity,
                facts={"classification": stop.classification, "reported_abnormal": stop.abnormal},
            )
        )
        if stop.abnormal:
            _append_once(report.classifications, stop.classification)

        if _to_bool(order.get("balance_insufficient_stop")):
            _append_once(report.classifications, "balance_insufficient")
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="余额限制标志",
                    observation="订单明确记录为余额不足停止",
                    severity=Severity.WARNING,
                )
            )

    def _analyze_transaction(
        self, order: dict[str, Any], report: DiagnosticReport
    ) -> tuple[dict[str, Any] | None, str | None]:
        tx_data = _json_object(order.get("tx_data"))
        received = _to_bool(order.get("is_receive_tx_data"))
        if not received:
            report.classifications.append("missing_tx_data")
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="交易结束数据",
                    observation="is_receive_tx_data=0，平台未确认收到设备交易数据",
                    severity=Severity.CRITICAL,
                )
            )
        elif tx_data is None:
            report.classifications.append("tx_data_inconsistent")
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="交易结束数据",
                    observation="接收标志为 1，但 tx_data 为空或无法解析",
                    severity=Severity.CRITICAL,
                )
            )
        else:
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="交易结束数据",
                    observation="设备交易数据已入库",
                    facts={"field_count": len(tx_data)},
                )
            )
            self._check_amounts(order, tx_data, report)

        tx_serial_no = None
        if tx_data:
            tx_serial_no = _clean_string(tx_data.get("txSerialNo"))
        if not tx_serial_no:
            tx_serial_no = _clean_string(order.get("transaction_id"))
        report.evidence.append(
            Evidence(
                source="mysql",
                title="设备交易流水号",
                observation="已取得流水号，可关联 TDengine"
                if tx_serial_no
                else "未取得流水号，将仅按设备和时间查询",
                severity=Severity.INFO if tx_serial_no else Severity.WARNING,
                facts={
                    "source": "tx_data.txSerialNo"
                    if tx_data and tx_data.get("txSerialNo")
                    else "transaction_id_or_none"
                },
            )
        )
        return tx_data, tx_serial_no

    def _check_amounts(
        self, order: dict[str, Any], tx_data: dict[str, Any], report: DiagnosticReport
    ) -> None:
        mismatches: list[str] = []
        negative_fields: list[str] = []
        missing_fields: list[str] = []
        for field in (
            "electricityQuantity",
            "totalFee",
            "tipFee",
            "peakFee",
            "flatFee",
            "valleyFee",
        ):
            value = _decimal(tx_data.get(field))
            if value is not None and value < 0:
                negative_fields.append(field)

        protocol = str(order.get("device_protocol") or "")
        server_billing = is_server_billing(protocol)
        order_energy = _decimal(order.get("electricity"))
        tx_energy = _decimal(tx_data.get("electricityQuantity"))
        if tx_energy is None and not server_billing:
            missing_fields.append("electricityQuantity")
        if not _near(order_energy, tx_energy, ENERGY_TOLERANCE):
            mismatches.append("设备总电量与订单电量不一致")

        tx_total = _decimal(tx_data.get("totalFee"))
        electricity_fee = _decimal(order.get("electricity_fee")) or Decimal(0)
        service_fee = _decimal(order.get("service_fee")) or Decimal(0)
        platform_core_fee = electricity_fee + service_fee
        period_total = sum(
            (_decimal(tx_data.get(field)) or Decimal(0))
            for field in ("tipFee", "peakFee", "flatFee", "valleyFee")
        )

        if not server_billing and tx_total is not None:
            if not _near(tx_total, platform_core_fee, AMOUNT_TOLERANCE):
                mismatches.append("桩上报总费用与平台电费加服务费不一致")
            if period_total and not _near(tx_total, period_total, AMOUNT_TOLERANCE):
                mismatches.append("桩上报分时费用之和与 totalFee 不一致")
            if tx_total < electricity_fee and service_fee == 0:
                mismatches.append("设备 totalFee 低于平台计算电费，服务费被源码钳制为 0")
        elif not server_billing:
            missing_fields.append("totalFee")

        expected_total = sum(
            (_decimal(order.get(field)) or Decimal(0))
            for field in (
                "electricity_fee",
                "service_fee",
                "launch_fee",
                "park_fee",
                "ds_electric_fee",
                "ds_service_fee",
            )
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        stored_total = _decimal(order.get("total_amount"))
        if stored_total is not None and not _near(expected_total, stored_total, AMOUNT_TOLERANCE):
            mismatches.append("订单 total_amount 与源码 collectFee 汇总公式不一致")

        if server_billing:
            meter_start = _decimal(order.get("meter_start"))
            meter_end = _decimal(order.get("meter_end")) or _decimal(tx_data.get("meterEndValue"))
            if meter_start is not None and meter_end is not None and meter_end >= meter_start:
                meter_energy = (meter_end - meter_start) / Decimal(1000)
                if not _near(meter_energy, order_energy, Decimal("0.02")):
                    mismatches.append("服务端计费电表差值与订单电量不一致")
            elif tx_energy is None:
                missing_fields.append("electricityQuantity_or_meter_values")

        if negative_fields:
            mismatches.append("设备交易数据包含负值")
        severity = Severity.CRITICAL if mismatches else Severity.WARNING if missing_fields else Severity.INFO
        if mismatches:
            _append_once(report.classifications, "amount_inconsistent")
        if missing_fields:
            _append_once(report.classifications, "transaction_data_incomplete")
        report.evidence.append(
            Evidence(
                source="mysql+business_rules",
                title="金额与电量一致性",
                observation=(
                    "；".join(mismatches)
                    if mismatches
                    else f"缺少字段 {missing_fields}，无法完成全部核对"
                    if missing_fields
                    else "未发现明显的金额或电量内部矛盾"
                ),
                severity=severity,
                facts={
                    "billing_side": "server" if server_billing else "pile",
                    "negative_fields": negative_fields,
                    "missing_fields": missing_fields,
                    "expected_total_amount": expected_total,
                },
            )
        )

    def _inspect_fee_snapshot(self, order: dict[str, Any], report: DiagnosticReport) -> None:
        try:
            record = self.sources.get_fee_template_record(report.request.order_no, report.request.tenant_id)
            report.queried_sources.append("mysql:ch_fee_template_record")
        except SourceError as exc:
            report.limitations.append(str(exc))
            return
        if record:
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="订单计费模板快照",
                    observation="存在订单级计费模板快照，应以快照而不是当前场地模板复核",
                    facts={"has_period_detail": bool(record.get("period_fee_detail"))},
                )
            )
        else:
            _append_once(report.classifications, "fee_snapshot_missing")
            if report.request.intent == Intent.AMOUNT:
                report.limitations.append("缺少订单级计费模板快照，无法高置信度重放历史计费")
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="订单计费模板快照",
                    observation="未找到订单级快照，源码会回退到当前场地模板，历史复核可能受模板变更影响",
                    severity=Severity.WARNING,
                )
            )

    def _inspect_device(self, order: dict[str, Any], report: DiagnosticReport) -> None:
        try:
            device = self.sources.get_device(
                _clean_string(order.get("device_id")), _clean_string(order.get("device_code"))
            )
            report.queried_sources.append("mysql:iot_charging_device")
        except SourceError as exc:
            report.limitations.append(str(exc))
            return
        if not device:
            report.evidence.append(
                Evidence(
                    source="mysql",
                    title="设备主数据",
                    observation="未找到设备主数据",
                    severity=Severity.WARNING,
                )
            )
            return
        online = _to_int(device.get("online_status"))
        report.evidence.append(
            Evidence(
                source="mysql",
                title="设备当前状态",
                observation="设备当前在线" if online == 1 else "设备当前离线或在线状态未知",
                severity=Severity.INFO if online == 1 else Severity.WARNING,
                facts={
                    "protocol": device.get("protocol"),
                    "online_status": online,
                    "work_status": device.get("work_status"),
                    "error_reason": device.get("error_reason"),
                },
            )
        )

    def _inspect_tdengine(
        self, order: dict[str, Any], tx_serial_no: str | None, report: DiagnosticReport
    ) -> None:
        window = _order_window(order, self.safety.max_order_window_hours)
        if not window:
            report.limitations.append("订单缺少可用的 created_time，未查询 TDengine")
            return
        start_time, end_time, clamped = window
        if clamped:
            report.limitations.append(
                f"TDengine 查询窗口按安全上限截断为 {self.safety.max_order_window_hours} 小时"
            )

        child_device = _clean_string(order.get("child_device_code"))
        if child_device:
            try:
                samples = self.sources.get_gun_samples(child_device, start_time, end_time, tx_serial_no)
                report.queried_sources.append("tdengine:charging-gun_property")
                self._analyze_gun_samples(samples, report)
            except (SourceError, ValueError) as exc:
                report.limitations.append(str(exc))
        else:
            report.limitations.append("订单缺少 child_device_code，未查询枪时序")

        device = _clean_string(order.get("device_code"))
        if device:
            try:
                messages = self.sources.get_comm_messages(device, start_time, end_time)
                report.queried_sources.append("tdengine:charging-pile_comm")
                self._analyze_comm(messages, report)
            except (SourceError, ValueError) as exc:
                report.limitations.append(str(exc))
        else:
            report.limitations.append("订单缺少 device_code，未查询通讯报文")

    def _analyze_gun_samples(self, samples: list[dict[str, Any]], report: DiagnosticReport) -> None:
        if not samples:
            report.evidence.append(
                Evidence(
                    source="tdengine",
                    title="充电枪过程时序",
                    observation="订单时间范围内未查询到枪状态数据",
                    severity=Severity.WARNING,
                )
            )
            _append_once(report.classifications, "gun_timeseries_missing")
            return
        statuses = _unique_sequence(_to_int(row.get("status")) for row in samples)
        errors = sorted(
            {
                str(row.get("errorCode"))
                for row in samples
                if row.get("errorCode") not in (None, "", "0", 0, 0.0)
            }
        )
        temperatures = [_to_float(row.get("temperature")) for row in samples]
        battery_temperatures = [_to_float(row.get("batteryMaxTemperature")) for row in samples]
        powers = [_to_float(row.get("power")) for row in samples]
        drops = sum(
            previous is not None
            and current is not None
            and previous > 1
            and current <= previous * 0.2
            and _to_int(current_row.get("status")) != 2
            for previous, current, current_row in zip(powers, powers[1:], samples[1:], strict=False)
        )
        max_temperature = _max_not_none(temperatures)
        max_battery_temperature = _max_not_none(battery_temperatures)
        severity = Severity.WARNING if errors or drops else Severity.INFO
        if errors:
            _append_once(report.classifications, "device_error_reported")
        if drops:
            _append_once(report.classifications, "power_drop_observed")
        report.evidence.append(
            Evidence(
                source="tdengine",
                title="充电枪过程时序",
                observation=f"查询到 {len(samples)} 个快照，状态轨迹 {statuses or ['unknown']}",
                severity=severity,
                facts={
                    "status_transitions": statuses,
                    "error_codes": errors[:20],
                    "power_drop_count": drops,
                    "max_temperature": max_temperature,
                    "max_battery_temperature": max_battery_temperature,
                    "final_insert_state": _to_int(samples[-1].get("isInsert")),
                },
            )
        )
        if len(samples) >= self.safety.tdengine_max_rows:
            report.limitations.append("枪时序结果达到行数上限，报告只分析了截断数据")

    def _analyze_comm(self, messages: list[dict[str, Any]], report: DiagnosticReport) -> None:
        if not messages:
            report.evidence.append(
                Evidence(
                    source="tdengine",
                    title="设备通讯报文",
                    observation="订单时间范围内未查询到设备通讯报文",
                    severity=Severity.WARNING,
                )
            )
            _append_once(report.classifications, "communication_missing")
            return
        directions: dict[str, int] = {}
        codes: dict[str, int] = {}
        error_messages = 0
        for message in messages:
            direction = str(message.get("direction"))
            code = str(message.get("code") or "unknown")
            directions[direction] = directions.get(direction, 0) + 1
            codes[code] = codes.get(code, 0) + 1
            decoded = str(message.get("decoded") or "").lower()
            if any(word in decoded for word in ("error", "fail", "fault", "异常", "失败", "故障")):
                error_messages += 1
        if error_messages:
            _append_once(report.classifications, "protocol_error_observed")
        report.evidence.append(
            Evidence(
                source="tdengine",
                title="设备通讯报文",
                observation=f"查询到 {len(messages)} 条报文，其中疑似错误语义 {error_messages} 条",
                severity=Severity.WARNING if error_messages else Severity.INFO,
                facts={"directions": directions, "codes": codes},
            )
        )
        if len(messages) >= self.safety.tdengine_max_rows:
            report.limitations.append("通讯报文结果达到行数上限，报告只分析了截断数据")

    def _inspect_redis(self, order: dict[str, Any], report: DiagnosticReport) -> None:
        try:
            streams = self.sources.inspect_streams(report.request.order_no)
            report.queried_sources.append("redis:order_sync_streams")
        except SourceError as exc:
            report.limitations.append(str(exc))
            return
        for stream in streams:
            matches = int(stream.get("matches") or 0)
            pending = sum(int(group.get("pending") or 0) for group in stream.get("groups", []))
            severity = Severity.WARNING if pending else Severity.INFO
            report.evidence.append(
                Evidence(
                    source="redis",
                    title=f"订单同步 Stream: {stream.get('stream')}",
                    observation=(
                        f"最近受限范围内命中订单 {matches} 次，消费组 pending={pending}；"
                        "Stream 保留消息，命中不等于仍未消费"
                    ),
                    severity=severity,
                    facts={
                        "length": stream.get("length"),
                        "matches": matches,
                        "pending": pending,
                        "groups": stream.get("groups", []),
                    },
                )
            )
            if pending:
                _append_once(report.classifications, "sync_pending")
        if report.request.intent == Intent.SYNC and not any(
            int(item.get("matches") or 0) for item in streams
        ):
            report.limitations.append(
                "最近 Redis 消息中未发现该订单，但消息可能已被裁剪或早于检查窗口，不能据此断定未发送"
            )

    def _finalize(
        self, order: dict[str, Any], tx_data: dict[str, Any] | None, report: DiagnosticReport
    ) -> None:
        priority = [
            "missing_tx_data",
            "uncontrollable_exception",
            "controlled_abnormal_end",
            "start_failure",
            "amount_inconsistent",
            "communication_missing",
            "device_error_reported",
            "sync_pending",
            "transaction_data_incomplete",
        ]
        if report.request.intent == Intent.AMOUNT:
            priority = ["amount_inconsistent", *priority]
        elif report.request.intent == Intent.START_FAILURE:
            priority = ["start_failure", "missing_tx_data", *priority]
        elif report.request.intent == Intent.SYNC:
            priority = ["sync_pending", *priority]
        primary = next((item for item in priority if item in report.classifications), None)
        summaries = {
            "missing_tx_data": "平台未确认收到设备交易结束数据，优先沿通讯和设备上报链路排查",
            "uncontrollable_exception": "订单属于不可控异常，需结合时序和通讯证据判断断网、离线或断电",
            "controlled_abnormal_end": "设备已上报结束事件，但停止原因被业务代码判定为异常结束",
            "start_failure": "订单停止原因指向启动失败",
            "amount_inconsistent": "订单金额或电量在设备上报与平台结果之间存在不一致",
            "communication_missing": "订单时间范围内未发现设备通讯报文",
            "device_error_reported": "充电枪时序记录了设备故障码",
            "sync_pending": "Redis 消费组存在待确认的订单同步消息",
            "transaction_data_incomplete": "设备交易数据字段不完整，无法完成全部金额和电量核对",
        }
        report.summary = summaries.get(primary, "全链路只读检查未发现可由现有规则确认的明显异常")

        strong_sources = {
            "mysql:ch_order_info",
            "tdengine:charging-gun_property",
            "tdengine:charging-pile_comm",
        }
        required_sources = set(strong_sources)
        if report.request.intent == Intent.SYNC:
            required_sources.add("redis:order_sync_streams")
        if report.request.intent == Intent.AMOUNT:
            required_sources.add("mysql:ch_fee_template_record")
        source_coverage = len(strong_sources.intersection(report.queried_sources))
        if (primary and source_coverage >= 2) or (
            not primary and not report.limitations and required_sources.issubset(report.queried_sources)
        ):
            report.confidence = "high"
        elif source_coverage >= 1:
            report.confidence = "medium"
        else:
            report.confidence = "low"

        if "missing_tx_data" in report.classifications:
            report.next_steps.append("人工核对订单结束时间附近是否存在设备交易结束上报和平台消费日志")
        if "amount_inconsistent" in report.classifications:
            report.next_steps.append("以订单计费模板快照复核分时电价，并将 tx_data 原始值提交设备厂商确认")
        if "transaction_data_incomplete" in report.classifications:
            report.next_steps.append("核对设备协议字段映射和交易结束事件原始报文")
        if any(item in report.classifications for item in ("device_error_reported", "power_drop_observed")):
            report.next_steps.append("根据故障码、温度和功率变化安排设备侧复核")
        if report.request.intent == Intent.SYNC:
            report.next_steps.append("结合商城消费者日志和订单 out_trade_no 判断同步是否真正完成")
        if not tx_data and _to_bool(order.get("is_receive_tx_data")):
            report.next_steps.append("检查 tx_data JSON 序列化或历史数据完整性")
        report.next_steps.append("所有处理动作由工程师确认后在现有业务后台执行，本工具不会自动操作")


def _order_window(order: dict[str, Any], max_hours: int) -> tuple[datetime, datetime, bool] | None:
    created = _datetime(order.get("created_time"))
    if not created:
        return None
    stopped = _datetime(order.get("stop_time")) or datetime.now(tz=created.tzinfo)
    start = created - timedelta(minutes=5)
    end = stopped + timedelta(minutes=5)
    maximum_end = start + timedelta(hours=max_hours)
    clamped = end > maximum_end
    return start, min(end, maximum_end), clamped


def _public_order_facts(order: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "order_no",
        "tenant_id",
        "status",
        "type",
        "billing_type",
        "launch_type",
        "is_test",
        "device_code",
        "child_device_code",
        "site_id",
        "device_protocol",
        "created_time",
        "stop_time",
        "electricity",
        "electricity_fee",
        "service_fee",
        "ds_electric_fee",
        "ds_service_fee",
        "launch_fee",
        "park_fee",
        "total_amount",
        "pay_amount",
        "is_pay",
        "is_receive_tx_data",
        "stopped_reason_code",
        "stopped_reason_content",
        "error_info",
        "balance_insufficient_stop",
    )
    result = {field: order.get(field) for field in fields}
    result["status_label"] = status_label(order.get("status"))
    return result


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _near(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None or right is None:
        return True
    return abs(left - right) <= tolerance


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_sequence(values: Any) -> list[int]:
    result: list[int] = []
    for value in values:
        if value is not None and (not result or result[-1] != value):
            result.append(value)
    return result


def _max_not_none(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
