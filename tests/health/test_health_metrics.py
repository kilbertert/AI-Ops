from __future__ import annotations

from aiops_diagnostics.health.health_curves import build_curves
from aiops_diagnostics.health.health_job_runner import normalize_tdengine_gun_samples
from aiops_diagnostics.health.health_metrics import (
    RULE_VERSION,
    calculate_soh,
    enrich_report,
    score_capacity,
    score_max_temperature,
    score_soc_balance,
    score_temperature_balance,
    score_voltage_balance,
)
from aiops_diagnostics.health.ykc_frames import (
    BATTERY_TYPE_LFP,
    BATTERY_TYPE_NCM,
    extract_gun_no,
    parse_ykc_comm_frames,
)


def _ykc_raw(
    cmd: str, payload_hex: str, *, tx_hex: str = "02125844025358872576000000000000", gun_hex: str = "01"
) -> str:
    pile_hex = "20260919000041"
    # YKC 帧头：68(1B)+长度(1B)+序列号(2B)+加密(1B)+帧类型(1B)+流水号(16B)+桩码(7B)+枪号(1B)+载荷+CRC(2B)
    body = f"000000{cmd}{tx_hex}{pile_hex}{gun_hex}{payload_hex}"
    length = len(bytes.fromhex(body))
    return f"68{length:02x}{body}0000"


def test_score_boundaries_and_shallow_charge_threshold() -> None:
    """五维评分全四段边界与规格 §2.3 浅充门槛（ΔSOC < 30% 不作有效 SOH 判定）。"""
    assert score_temperature_balance(2) == 100.0
    assert score_temperature_balance(5) == 82.0
    assert score_temperature_balance(8) == 52.0
    assert score_temperature_balance(10) == 26.0
    assert score_temperature_balance(15) == 0.0

    assert score_max_temperature(35) == 100.0
    assert score_max_temperature(45) == 82.0
    assert score_max_temperature(55) == 32.0
    assert score_max_temperature(55.1) == 0.0

    assert score_capacity(None) is None
    assert score_capacity(95) == 100.0
    assert score_capacity(80) == 80.0
    assert score_capacity(70) == 60.0

    assert score_voltage_balance(30) == 100.0
    assert score_voltage_balance(80) == 82.0
    assert score_voltage_balance(150) == 50.5
    assert score_voltage_balance(200) == 20.5

    assert score_soc_balance(1, 1) == 55.0

    # 规格 §2.3：ΔSOC < 30% 判浅充，默认返回 None；允许浅充估算时才给出参考值
    assert calculate_soh(20, 20, 50) is None
    assert calculate_soh(20, 20, 50, allow_shallow=True) == 188.0
    assert calculate_soh(20, 5, 50) is None
    # ΔSOC >= 30%：45.4 / 0.81 * 0.94 / 59.0 * 100 = 89.3
    assert calculate_soh(45.4, 81, 59.0) == 89.3


def test_enrich_report_same_frame_temp_delta_and_shallow_charge() -> None:
    """同帧温差 max(T_max - T_min) 与 ΔSOC=20% 浅充标记（ADR-0352）。"""
    report = {
        "rule_version": RULE_VERSION,
        "indicators": [
            {
                "code": "stop_reason",
                "status": "normal",
                "value": "车端正常主动停止",
                "reported_value": "车端正常主动停止",
                "reported_language": None,
                "unit": None,
                "reference": None,
                "reason_code": None,
            }
        ],
        "source_summary": {
            "order_snapshot": "available",
            "telemetry": "available",
            "protocol": "unavailable",
            "vehicle_capacity": "not_requested",
        },
    }
    samples = [
        {
            "_ts": 0,
            "batteryMaxTemperature": 30,
            "batteryMinTemperature": 28,
            "soc": 20,
            "chargingElectricityQuantity": 1,
            "cellMaxVoltage": 4000,
        },
        {
            "_ts": 600,
            "batteryMaxTemperature": 32,
            "batteryMinTemperature": 29,
            "soc": 40,
            "chargingElectricityQuantity": 11,
            "cellMaxVoltage": 4050,
        },
    ]

    result = enrich_report(report, samples, {"nominal_capacity_kwh": 50.0})

    assert result["rule_version"] == "health-v3"
    # 同帧最大温差为 max(30-28, 32-29) = 3.0，而非跨全程 32 - 28 = 4.0
    assert result["health_metrics"]["temperature_delta"] == 3.0
    assert result["health_metrics"]["soc_delta"] == 20.0
    assert result["health_metrics"]["soh"] is None
    assert result["health_metrics"]["soh_status"] == "shallow_charge"
    assert result["health_metrics"]["is_shallow_charge"] is True
    assert result["health_metrics"]["soh_shallow_estimate"] == 94.0
    assert result["overall_health_score"] is None
    assert result["overall_health_source"] == "soh"
    assert {item["code"] for item in result["radar"]} == {
        "temperature_balance",
        "max_temperature",
        "capacity",
        "voltage_balance",
        "soc_balance",
    }
    cap = next(item for item in result["radar"] if item["code"] == "capacity")
    assert cap["status"] == "unavailable"
    assert cap["reason_code"] == "SHALLOW_CHARGE"
    assert len(result["indicators"]) == 7
    assert "shallow_charge" in result["conclusion"]["advice_codes"]


def test_ykc_frame_decoder_filters_dual_gun_and_sentinels() -> None:
    """YKC 协议帧解码验证（基于 41 实测字节布局）及双枪防串单/哨兵过滤。"""
    assert extract_gun_no("20260919000041", "20260919000041-1") == 1
    assert extract_gun_no("20260919000041", "20260919000041-2") == 2

    rows = [
        # 枪 1：0x15 LFP (0x03)
        {"_ts": "2026-11-26T06:00:00Z", "code": "15", "raw": _ykc_raw("15", "010100032607970cffffffff")},
        # 枪 2 干扰帧：0x15 NCM (0x06) + 0x17 100kWh，必须被 gun_no=1 过滤
        {
            "_ts": "2026-11-26T06:00:01Z",
            "code": "15",
            "raw": _ykc_raw(
                "15", "010100062607970cffffffff", gun_hex="02", tx_hex="99999999999999999999000000000000"
            ),
        },
        {
            "_ts": "2026-11-26T06:00:02Z",
            "code": "17",
            "raw": _ykc_raw(
                "17", "6e018403e803c40e6a1a02110d", gun_hex="02", tx_hex="99999999999999999999000000000000"
            ),
        },
        # 枪 1：0x17 标称总能量 0x024e = 590 -> 59.0 kWh
        {
            "_ts": "2026-11-26T06:00:03Z",
            "code": "17",
            "raw": _ykc_raw("17", "6e0184034e02c40e6a1a02110d1027d0072e049001"),
        },
        # 枪 1：0x23 单体最高压 57 f1 -> 0xf157 -> 低12位 0x157=343 -> 3.43V，组号 15，SOC 85%
        {
            "_ts": "2026-11-26T06:05:00Z",
            "code": "23",
            "raw": _ykc_raw("23", "c40e130a029e0d0c0a57f1551600ac0d0a0a1000"),
        },
        # 枪 1：0x25 探针 0x47 -> #72，T_max=0x52(32℃)，T_min=0x4e(28℃)，允许充电 [17]=0x01
        {
            "_ts": "2026-11-26T06:05:01Z",
            "code": "25",
            "raw": _ykc_raw("25", "4752004e0c00000000000000000000000001000000"),
        },
        # 枪 1：0x25 SOC 异常告警 [7:9]=0x0100
        {
            "_ts": "2026-11-26T06:10:01Z",
            "code": "25",
            "raw": _ykc_raw("25", "475500500c00000100000000000000000001000000"),
        },
        # 枪 1：0x19 结算帧 V_min=3.42V (56 01), V_max=3.47V (5b 01) -> 50 mV; T_min=31℃, T_max=33℃
        {
            "_ts": "2026-11-26T06:30:00Z",
            "code": "19",
            "raw": _ykc_raw("19", "6356015b0151532300ca3800000000"),
        },
    ]

    ev = parse_ykc_comm_frames(rows, gun_no=1, tx_serial_no="2125844025358872576")
    assert ev.battery_type == BATTERY_TYPE_LFP
    assert ev.nominal_energy_kwh == 59.0
    assert ev.end_min_cell_voltage_v == 3.42
    assert ev.end_max_cell_voltage_v == 3.47
    assert ev.end_min_temp_c == 31.0
    assert ev.end_max_temp_c == 33.0
    assert len(ev.voltage_points) == 1
    assert ev.voltage_points[0].max_cell_voltage_v == 3.43
    assert ev.voltage_points[0].max_cell_group == 15
    assert ev.voltage_points[0].bms_measured_voltage_v == 348.6
    assert len(ev.temp_points) == 2
    assert ev.temp_points[0].max_cell_probe_no == 72
    assert ev.temp_points[0].max_temp_c == 32.0
    assert ev.temp_points[0].min_temp_c == 28.0
    assert ev.soc_alarm_count == 1

    # 0x19 结算帧 V_min=0x0000 哨兵必须被过滤，不产生 3490 mV 假极差
    sentinel_ev = parse_ykc_comm_frames(
        [
            {
                "_ts": "2026-11-26T06:30:00Z",
                "code": "19",
                "raw": _ykc_raw("19", "3600005d0152580b00920000000001"),
            }
        ],
        gun_no=1,
    )
    assert sentinel_ev.end_min_cell_voltage_v is None
    assert sentinel_ev.end_max_cell_voltage_v == 3.49


def test_sliding_window_avoids_quantization_false_alarm_and_catches_real_anomalies() -> None:
    """5 分钟温升滑窗避免 1℃/15s 量化误报，同时能捕获真实过快温升与 15s SOC 跳变。"""
    # 正常充电：10 分钟内温度仅在第 15s 跳 1℃（30 -> 31），总温升 0.1℃/min <= 0.3
    normal_samples = [
        {
            "_ts": 0,
            "batteryMaxTemperature": 30,
            "batteryMinTemperature": 28,
            "soc": 20,
            "chargingElectricityQuantity": 0.0,
        },
        {
            "_ts": 15,
            "batteryMaxTemperature": 31,
            "batteryMinTemperature": 29,
            "soc": 20,
            "chargingElectricityQuantity": 0.5,
        },
        {
            "_ts": 300,
            "batteryMaxTemperature": 31,
            "batteryMinTemperature": 29,
            "soc": 40,
            "chargingElectricityQuantity": 15.0,
        },
        {
            "_ts": 600,
            "batteryMaxTemperature": 31,
            "batteryMinTemperature": 29,
            "soc": 60,
            "chargingElectricityQuantity": 30.0,
        },
    ]
    normal_report = enrich_report(
        {"indicators": [], "source_summary": {}},
        normal_samples,
        {"nominal_capacity_kwh": 75.0, "battery_type": BATTERY_TYPE_NCM},
    )
    normal_map = {item["code"]: item for item in normal_report["indicators"]}
    assert normal_map["temp_rise_rate"]["status"] == "normal"
    assert normal_map["temp_rise_rate"]["value"] == 0.2
    assert normal_map["soc_change_rate"]["status"] == "normal"
    assert normal_report["overall_health_score"] == 94.0
    assert normal_report["conclusion"]["level"] == "normal"

    # 异常充电：5 分钟升温 4℃（0.8℃/min > 0.3）、最高温 56℃（> 55）、10s 内 SOC 跳变 5%（>= 3%）
    abnormal_samples = [
        {
            "_ts": 0,
            "batteryMaxTemperature": 52,
            "batteryMinTemperature": 45,
            "soc": 20,
            "chargingElectricityQuantity": 0.0,
        },
        {
            "_ts": 10,
            "batteryMaxTemperature": 52,
            "batteryMinTemperature": 45,
            "soc": 25,
            "chargingElectricityQuantity": 1.0,
        },
        {
            "_ts": 300,
            "batteryMaxTemperature": 56,
            "batteryMinTemperature": 49,
            "soc": 55,
            "chargingElectricityQuantity": 20.0,
        },
    ]
    abnormal_report = enrich_report(
        {"indicators": [], "source_summary": {}},
        abnormal_samples,
        {"nominal_capacity_kwh": 60.0},
    )
    ab_map = {item["code"]: item for item in abnormal_report["indicators"]}
    assert ab_map["battery_temp_delta"]["status"] == "abnormal"
    assert ab_map["temp_rise_rate"]["status"] == "abnormal"
    assert ab_map["battery_max_temp"]["status"] == "abnormal"
    assert ab_map["soc_change_rate"]["status"] == "abnormal"
    assert abnormal_report["health_metrics"]["soc_jumps_15s"] == 1
    assert abnormal_report["conclusion"]["level"] == "abnormal"
    assert "thermal" in abnormal_report["conclusion"]["advice_codes"]
    assert "soc" in abnormal_report["conclusion"]["advice_codes"]


def test_pre_handshake_zero_rows_do_not_corrupt_soh_or_rates() -> None:
    """41 实测握手前全零行 (I=0, soc=0, T=0/-50, E=0) 不得污染 SOH、SOC 跳变与温升速率。"""
    samples = [
        {
            "_ts": 0,
            "outputCurrent": 0,
            "soc": 0,
            "batteryMaxTemperature": 0,
            "chargingElectricityQuantity": 0,
        },
        {
            "_ts": 10,
            "outputCurrent": 0,
            "soc": 0,
            "batteryMaxTemperature": -50,
            "chargingElectricityQuantity": 0,
        },
        {
            "_ts": 20,
            "outputCurrent": 105.0,
            "soc": 51,
            "batteryMaxTemperature": 24,
            "chargingElectricityQuantity": 0.1,
        },
        {
            "_ts": 320,
            "outputCurrent": 100.0,
            "soc": 70,
            "batteryMaxTemperature": 25,
            "chargingElectricityQuantity": 6.5,
        },
        {
            "_ts": 920,
            "outputCurrent": 30.0,
            "soc": 98,
            "batteryMaxTemperature": 25,
            "chargingElectricityQuantity": 14.6,
        },
    ]
    report = enrich_report(
        {"indicators": [], "source_summary": {}},
        samples,
        {"nominal_capacity_kwh": 32.0},
    )
    ind = {item["code"]: item for item in report["indicators"]}
    assert report["overview"]["start_soc"] == 51.0
    assert report["overview"]["end_soc"] == 98.0
    assert report["health_metrics"]["soc_delta"] == 47.0
    assert report["health_metrics"]["soc_jumps_15s"] == 0
    assert ind["soc_change_rate"]["status"] == "normal"
    assert ind["temp_rise_rate"]["status"] == "normal"
    assert report["overall_health_score"] is not None
    assert 85.0 <= report["overall_health_score"] <= 95.0


def test_tdengine_power_normalization_and_iso_curve_sentinels() -> None:
    """TDengine 瓦特功率换算为 kW，ISO 时间戳与 -50℃ 哨兵在曲线中正确处理。"""
    raw_rows = [
        {
            "_ts": "2026-11-26T06:00:00Z",
            "outputVoltage": 350.0,
            "outputCurrent": 100.0,
            "power": 35000.0,
            "batteryMaxTemperature": -50,
        },
        {
            "_ts": "2026-11-26T06:05:00Z",
            "outputVoltage": 360.0,
            "outputCurrent": 100.0,
            "power": 36000.0,
            "batteryMaxTemperature": 32,
        },
    ]
    normalized = normalize_tdengine_gun_samples(raw_rows)
    assert normalized[0]["power"] == 35.0
    assert normalized[1]["power"] == 36.0

    curves = build_curves(normalized, "zh")
    power_pts = curves["power"]["series"][0]["points"]
    assert len(power_pts) == 2
    assert power_pts[0][1] == 35.0
    # -50℃ 哨兵行必须被过滤，只保留有效 32℃ 点
    temp_pts = curves["temperature"]["series"][0]["points"]
    assert len(temp_pts) == 1
    assert temp_pts[0][1] == 32.0
