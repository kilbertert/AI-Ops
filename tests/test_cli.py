import json
import os
from pathlib import Path

from rich.text import Text
from typer.testing import CliRunner

from aiops_diagnostics.cli import app

FIXTURE = Path(__file__).parents[1] / "examples" / "fixtures" / "ocpp_consistent.json"


def test_cli_json_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "diagnose",
            "订单 TEST-OCPP-0003 金额是否正常",
            "--mode",
            "deterministic",
            "--fixture",
            str(FIXTURE),
            "--json",
        ],
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
        [
            "diagnose",
            "订单 TEST-OCPP-0003 金额是否正常",
            "--mode",
            "deterministic",
            "--fixture",
            str(fixture),
        ],
    )

    assert result.exit_code == 2
    assert "初始化失败" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_initializes_portable_home_and_installs_key(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "portable-home"
    config = portable_home / "custom.env"
    environment = {"AIOPS_HOME": str(portable_home)}

    initialized = runner.invoke(
        app,
        ["--config", str(config), "init"],
        env=environment,
    )

    assert initialized.exit_code == 0
    assert config.is_file()
    source = tmp_path / "provider.key"
    source.write_text("provider-secret", encoding="utf-8")
    installed = runner.invoke(
        app,
        ["--config", str(config), "key-install", "primary", "--from-file", str(source)],
        env=environment,
    )

    assert installed.exit_code == 0
    payload = json.loads(installed.stdout)
    assert payload["key_slot"] == "primary"
    assert "provider-secret" not in installed.stdout
    key_file = portable_home / "keys" / "primary.key"
    assert key_file.read_text(encoding="utf-8").strip() == "provider-secret"
    if os.name != "nt":
        assert key_file.stat().st_mode & 0o077 == 0


def test_diagnose_unified_exposes_mode_and_progress() -> None:
    result = CliRunner().invoke(app, ["diagnose", "--help"], color=True)
    output = Text.from_ansi(result.stdout).plain

    assert result.exit_code == 0
    assert "--mode" in output
    assert "deterministic" in output
    assert "--progress" in output
    assert "--no-progress" in output


def test_agent_diagnose_command_removed() -> None:
    result = CliRunner().invoke(app, ["agent-diagnose", "--help"])

    # --help exits 0 for any registered command; non-zero proves agent-diagnose
    # was removed (the unknown-command usage error is printed to stderr).
    assert result.exit_code != 0


def test_diagnose_agent_mode_without_key_suggests_deterministic(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "nokey-home"
    config = portable_home / "custom.env"
    environment = {"AIOPS_HOME": str(portable_home), "AIOPS_CODEX_API_KEY": ""}

    initialized = runner.invoke(app, ["--config", str(config), "init"], env=environment)
    assert initialized.exit_code == 0

    result = runner.invoke(
        app,
        ["--config", str(config), "diagnose", "订单 TEST-YKC-0001 金额异常"],
        env=environment,
    )

    assert result.exit_code == 2
    assert "--mode deterministic" in result.stdout


def test_cli_exposes_remote_gateway_commands() -> None:
    result = CliRunner().invoke(app, ["remote", "--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "enroll" in result.stdout
    assert "diagnose" in result.stdout
    assert "events" in result.stdout
