from pathlib import Path

from typer.testing import CliRunner

from aiops_diagnostics.cli import app

FIXTURE = Path(__file__).parents[1] / "examples" / "fixtures" / "ocpp_consistent.json"


def test_cli_json_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["diagnose", "订单 TEST-OCPP-0003 金额是否正常", "--fixture", str(FIXTURE), "--json"],
    )
    assert result.exit_code == 0
    assert "TEST-OCPP-0003" in result.stdout
    assert '"confidence": "high"' in result.stdout


def test_cli_malformed_fixture_fails_without_traceback(tmp_path: Path) -> None:
    fixture = tmp_path / "broken.json"
    fixture.write_text("{not-json", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["diagnose", "订单 TEST-OCPP-0003 金额是否正常", "--fixture", str(fixture)],
    )

    assert result.exit_code == 2
    assert "初始化失败" in result.stdout
    assert "Traceback" not in result.stdout
