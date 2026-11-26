"""YKC 1.6 充电协议帧纯函数解码器（ADR-0352 / Issue #602）。

解析 TDengine ``charging-pile_comm`` 中的五类 BMS 报文：
- ``0x15`` 握手帧：电池化学类型（03H=磷酸铁锂、06H=三元锂）
- ``0x17`` 参数配置帧：动力蓄电池标称总能量（BIN × 0.1 kWh）、单体最高允许电压
- ``0x19`` 充电结束帧：结算单体最低/最高电压（BIN × 0.01 V）、结算最低/最高温度（BIN − 50 ℃）
- ``0x23`` 充电需求帧：BMS 充电电压测量值（0.1 V）、单体最高压（低 12 位 × 0.01 V）与组号（高 4 位）、SOC
- ``0x25`` BMS 信息帧：最高单体电压探针号（BIN + 1）、最高/最低电池温度（BIN − 50 ℃）、序号 10 SOC 异常告警

双枪桩共用 ``charging-pile_comm.device``（桩编码），因此解码前必须按枪号字节与
``txSerialNo`` 严格过滤，避免同桩双枪并发充电时串单。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.common.scalar_rules import as_datetime

_YKC_HEADER_MAGIC = 0x68
_YKC_MIN_FRAME_BYTES = 34  # 6B 头 + 16B 流水号 + 7B 桩码 + 1B 枪号 + >=2B 载荷 + 2B CRC
_YKC_PAYLOAD_OFFSET = 30
_YKC_CRC_BYTES = 2
_GUN_SUFFIX_RE = re.compile(r"[-_:](\d{1,2})$")

BATTERY_TYPE_LFP = "lfp"
BATTERY_TYPE_NCM = "ncm"
_BATTERY_TYPE_MAP = {
    0x03: BATTERY_TYPE_LFP,
    0x06: BATTERY_TYPE_NCM,
}


@dataclass(frozen=True, slots=True)
class YkcBmsTempPoint:
    ts_seconds: float
    max_temp_c: float | None
    min_temp_c: float | None
    max_cell_probe_no: int | None
    soc_alarm: bool


@dataclass(frozen=True, slots=True)
class YkcBmsVoltagePoint:
    ts_seconds: float
    bms_measured_voltage_v: float | None
    max_cell_voltage_v: float | None
    max_cell_group: int | None
    soc: float | None


@dataclass(slots=True)
class YkcProtocolEvidence:
    matched_frames: int = 0
    battery_type: str | None = None
    nominal_energy_kwh: float | None = None
    max_allowed_cell_voltage_v: float | None = None
    end_soc: float | None = None
    end_min_cell_voltage_v: float | None = None
    end_max_cell_voltage_v: float | None = None
    end_min_temp_c: float | None = None
    end_max_temp_c: float | None = None
    temp_points: list[YkcBmsTempPoint] = field(default_factory=list)
    voltage_points: list[YkcBmsVoltagePoint] = field(default_factory=list)
    soc_alarm_count: int = 0

    @property
    def has_data(self) -> bool:
        return self.matched_frames > 0


def extract_gun_no(device_code: str | None, child_device_code: str | None) -> int | None:
    """从 ``child_device_code`` 解析 1-based 枪号（如 ``PILE-1`` -> 1、``PILE-02`` -> 2）。"""
    child = (child_device_code or "").strip()
    if not child:
        return None
    pile = (device_code or "").strip()
    if pile and child.startswith(pile) and len(child) > len(pile):
        tail = child[len(pile) :].lstrip("-_:")
        if tail.isdigit():
            value = int(tail)
            return value if 1 <= value <= 99 else None
    match = _GUN_SUFFIX_RE.search(child)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 99 else None
    return None


def read_tx_serial_no(order: Mapping[str, Any]) -> str | None:
    """从订单 ``tx_data.txSerialNo`` 或 ``transaction_id`` 读取数字交易流水号。"""
    tx_data = order.get("tx_data")
    if isinstance(tx_data, str) and tx_data.strip():
        try:
            tx_data = json.loads(tx_data)
        except ValueError:
            tx_data = None
    if isinstance(tx_data, Mapping):
        serial = str(tx_data.get("txSerialNo") or "").strip()
        if serial:
            return serial
    tx_id = str(order.get("transaction_id") or "").strip()
    return tx_id or None


def parse_ykc_comm_frames(
    rows: Iterable[Mapping[str, Any]],
    *,
    gun_no: int | None = None,
    tx_serial_no: str | None = None,
) -> YkcProtocolEvidence:
    """解码并过滤订单窗口内的 YKC 1.6 协议帧。"""
    evidence = YkcProtocolEvidence()
    expected_tx = _normalize_tx(tx_serial_no)

    for row in rows:
        raw_hex = str(row.get("raw") or "").strip()
        if len(raw_hex) < _YKC_MIN_FRAME_BYTES * 2 or len(raw_hex) % 2 != 0:
            continue
        try:
            frame = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        if len(frame) < _YKC_MIN_FRAME_BYTES or frame[0] != _YKC_HEADER_MAGIC:
            continue

        frame_gun = _decode_bcd_byte(frame[29])
        if gun_no is not None and frame_gun is not None and frame_gun != gun_no:
            continue

        frame_tx = frame[6:22].hex()
        if expected_tx and not _tx_matches(frame_tx, expected_tx):
            continue

        cmd = f"{frame[5]:02x}"
        row_code = str(row.get("code") or "").strip().lower()
        if row_code and row_code != cmd:
            continue

        payload = frame[_YKC_PAYLOAD_OFFSET:-_YKC_CRC_BYTES]
        ts_seconds = _parse_ts_seconds(row.get("_ts"))

        if cmd == "15":
            _decode_0x15(payload, evidence)
            evidence.matched_frames += 1
        elif cmd == "17":
            _decode_0x17(payload, evidence)
            evidence.matched_frames += 1
        elif cmd == "19":
            _decode_0x19(payload, evidence)
            evidence.matched_frames += 1
        elif cmd == "23":
            if ts_seconds is not None:
                point = _decode_0x23(payload, ts_seconds)
                if point is not None:
                    evidence.voltage_points.append(point)
                    evidence.matched_frames += 1
        elif cmd == "25":
            if ts_seconds is not None:
                point = _decode_0x25(payload, ts_seconds)
                if point is not None:
                    evidence.temp_points.append(point)
                    if point.soc_alarm:
                        evidence.soc_alarm_count += 1
                    evidence.matched_frames += 1

    evidence.temp_points.sort(key=lambda item: item.ts_seconds)
    evidence.voltage_points.sort(key=lambda item: item.ts_seconds)
    return evidence


def _decode_0x15(payload: bytes, evidence: YkcProtocolEvidence) -> None:
    if len(payload) < 4:
        return
    battery_type = _BATTERY_TYPE_MAP.get(payload[3])
    if battery_type is not None:
        evidence.battery_type = battery_type


def _decode_0x17(payload: bytes, evidence: YkcProtocolEvidence) -> None:
    if len(payload) < 6:
        return
    max_allowed_cell = _u16_le(payload, 0) * 0.01
    if 1.0 <= max_allowed_cell <= 6.0:
        evidence.max_allowed_cell_voltage_v = round(max_allowed_cell, 3)
    raw_energy = _u16_le(payload, 4)
    if raw_energy not in (0, 0xFFFF):
        nominal_kwh = raw_energy * 0.1
        if 5.0 <= nominal_kwh <= 1000.0:
            evidence.nominal_energy_kwh = round(nominal_kwh, 2)


def _decode_0x19(payload: bytes, evidence: YkcProtocolEvidence) -> None:
    if len(payload) < 7:
        return
    soc = payload[0]
    if 0 <= soc <= 100:
        evidence.end_soc = float(soc)
    v_min_raw = _u16_le(payload, 1)
    v_max_raw = _u16_le(payload, 3)
    v_min = v_min_raw * 0.01 if v_min_raw not in (0, 0xFFFF) else None
    v_max = v_max_raw * 0.01 if v_max_raw not in (0, 0xFFFF) else None
    if v_min is not None and 0.5 <= v_min <= 6.0:
        evidence.end_min_cell_voltage_v = round(v_min, 3)
    if v_max is not None and 0.5 <= v_max <= 6.0:
        evidence.end_max_cell_voltage_v = round(v_max, 3)
    t_min = _decode_temp_byte(payload[5])
    t_max = _decode_temp_byte(payload[6])
    if t_min is not None:
        evidence.end_min_temp_c = t_min
    if t_max is not None:
        evidence.end_max_temp_c = t_max


def _decode_0x23(payload: bytes, ts_seconds: float) -> YkcBmsVoltagePoint | None:
    if len(payload) < 12:
        return None
    measured_raw = _u16_le(payload, 5)
    measured_v = round(measured_raw * 0.1, 2) if measured_raw not in (0, 0xFFFF) else None
    cell_raw = _u16_le(payload, 9)
    cell_v_raw = cell_raw & 0x0FFF
    cell_v = round(cell_v_raw * 0.01, 3) if 50 <= cell_v_raw <= 600 else None
    group = (cell_raw >> 12) & 0x0F if cell_v is not None else None
    soc_raw = payload[11]
    soc = float(soc_raw) if 0 <= soc_raw <= 100 else None
    if measured_v is None and cell_v is None and soc is None:
        return None
    return YkcBmsVoltagePoint(
        ts_seconds=ts_seconds,
        bms_measured_voltage_v=measured_v,
        max_cell_voltage_v=cell_v,
        max_cell_group=group,
        soc=soc,
    )


def _decode_0x25(payload: bytes, ts_seconds: float) -> YkcBmsTempPoint | None:
    if len(payload) < 5:
        return None
    # GB/T 27930 BSM 序号 4：偏移量 +1，范围 1~256（0xFF 常用于缺省标记时按 256 边界排除）
    probe_byte = payload[0]
    probe_no = probe_byte + 1 if probe_byte != 0xFF else None
    t_max = _decode_temp_byte(payload[1])
    t_min = _decode_temp_byte(payload[3])
    if t_max is not None and t_min is not None and t_max < t_min:
        t_min = None
    # YKC 1.6 0x25 载荷 21 字节：[5..20] 为 8 组 2B 小端状态标志，
    # 实测 41 上 [17:19]==0x0001 为允许充电（序号 15），故序号 10 SOC 告警在 [7:9]（1=过高, 2=过低）
    soc_alarm = False
    if len(payload) >= 9:
        soc_flag = _u16_le(payload, 7)
        soc_alarm = soc_flag in (1, 2)
    if t_max is None and t_min is None and not soc_alarm:
        return None
    return YkcBmsTempPoint(
        ts_seconds=ts_seconds,
        max_temp_c=t_max,
        min_temp_c=t_min,
        max_cell_probe_no=probe_no,
        soc_alarm=soc_alarm,
    )


def _u16_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], byteorder="little", signed=False)


def _decode_temp_byte(value: int) -> float | None:
    # 0x00 (-50℃) 与 0xFF (+205℃) 是协议无探针/无效哨兵
    if value in (0x00, 0xFF):
        return None
    temp = float(value - 50)
    return temp if -40.0 <= temp <= 120.0 else None


def _decode_bcd_byte(value: int) -> int | None:
    high = (value >> 4) & 0x0F
    low = value & 0x0F
    if high <= 9 and low <= 9:
        return high * 10 + low
    return value


def _normalize_tx(value: str | None) -> str | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if len(text) < 8 or not text.isdigit():
        return None
    return text.lstrip("0") or None


def _tx_matches(frame_tx_hex: str, expected_tx: str) -> bool:
    stripped = frame_tx_hex.strip("0")
    if not stripped:
        # 握手或早期帧可能尚未填充交易流水号，此时退回枪号过滤
        return True
    return expected_tx in frame_tx_hex or stripped == expected_tx.rstrip("0")


def _parse_ts_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000.0 if abs(numeric) > 1e11 else numeric
    dt = as_datetime(value)
    if dt is not None:
        return dt.timestamp()
    try:
        numeric = float(value)
        return numeric / 1000.0 if abs(numeric) > 1e11 else numeric
    except (TypeError, ValueError):
        return None
