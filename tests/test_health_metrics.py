from aiops_diagnostics.health_metrics import (
    calculate_soh,
    enrich_report,
    score_capacity,
    score_max_temperature,
    score_soc_balance,
    score_temperature_balance,
    score_voltage_balance,
)


def test_score_boundaries_and_missing_capacity():
    assert score_temperature_balance(2) == 100
    assert score_temperature_balance(5) == 82
    assert score_max_temperature(35) == 100
    assert score_capacity(None) is None
    assert score_voltage_balance(30) == 100
    assert score_soc_balance(1, 1) == 55
    assert calculate_soh(20, 20, 50) == 188.0
    assert calculate_soh(20, 5, 50) is None


def test_enrich_report_returns_scores_and_unavailable_capacity() -> None:
    report = {"rule_version": "health-v1"}
    samples = [
        {
            "batteryMaxTemperature": 30,
            "batteryMinTemperature": 28,
            "soc": 20,
            "chargingElectricityQuantity": 1,
            "cellMaxVoltage": 4000,
        },
        {
            "batteryMaxTemperature": 32,
            "batteryMinTemperature": 29,
            "soc": 40,
            "chargingElectricityQuantity": 5,
            "cellMaxVoltage": 4050,
        },
    ]

    result = enrich_report(report, samples, {})

    assert result["rule_version"] == "health-v2"
    assert result["health_metrics"]["temperature_delta"] == 4
    assert result["health_metrics"]["soc_delta"] == 20
    assert result["health_metrics"]["soh"] is None
    assert result["health_metrics"]["soh_status"] == "unavailable"
    assert {item["code"] for item in result["radar"]} == {
        "temperature_balance",
        "max_temperature",
        "capacity",
        "voltage_balance",
        "soc_balance",
    }
    assert next(item for item in result["radar"] if item["code"] == "capacity")["status"] == "unavailable"
