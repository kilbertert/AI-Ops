from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and smoke-test the portable AI-Ops CLI bundle")
    parser.add_argument("--skip-build", action="store_true", help="Only smoke-test and archive dist/aiops")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    if not args.skip_build:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                str(project_root / "packaging/aiops.spec"),
            ],
            cwd=project_root,
            check=True,
        )

    distribution = project_root / "dist" / "aiops"
    _materialize_user_files(distribution)
    _assert_bundled_references(distribution)
    executable = distribution / ("aiops.exe" if os.name == "nt" else "aiops")
    fixture = distribution / "examples" / "fixtures" / "ocpp_consistent.json"
    if not executable.is_file() or not fixture.is_file():
        raise SystemExit("portable distribution is missing the executable or smoke fixture")

    with tempfile.TemporaryDirectory(prefix="aiops-portable-smoke-") as temporary_home:
        temporary_root = Path(temporary_home)
        environment = os.environ.copy()
        environment["AIOPS_HOME"] = str(temporary_root / "home")
        environment["PYTHONUTF8"] = "1"
        _run([str(executable), "--help"], environment, distribution)
        _run([str(executable), "init"], environment, distribution)
        _run([str(executable), "paths"], environment, distribution)
        smoke_key = temporary_root / "provider.key"
        smoke_key.write_text("portable-smoke-provider-key\n", encoding="utf-8")
        _run(
            [str(executable), "key-install", "smoke", "--from-file", str(smoke_key)],
            environment,
            distribution,
        )
        _run(
            [str(executable), "agent-doctor", "--key-slot", "smoke"],
            environment,
            distribution,
        )
        bundled_codex = (
            distribution
            / "_internal"
            / "codex_cli_bin"
            / "bin"
            / ("codex.exe" if os.name == "nt" else "codex")
        )
        _run(
            [str(executable), "__codex-launcher", str(bundled_codex), "--version"],
            environment,
            distribution,
        )
        _run(
            [
                str(executable),
                "diagnose",
                "订单 TEST-OCPP-0003 金额是否正常",
                "--fixture",
                str(fixture),
                "--json",
            ],
            environment,
            distribution,
        )

    _assert_no_embedded_secrets(distribution)
    version = _project_version(project_root / "pyproject.toml")
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64")
    archive_base = project_root / "dist" / f"aiops-diagnostics-{version}-{system}-{machine}"
    archive = Path(str(archive_base) + ".zip")
    if archive.exists():
        archive.unlink()
    shutil.make_archive(str(archive_base), "zip", distribution.parent, distribution.name)
    print(f"portable archive: {archive}")


def _run(command: list[str], environment: dict[str, str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _materialize_user_files(distribution: Path) -> None:
    internal = distribution / "_internal"
    for name in (".env.example", "README.md"):
        source = internal / name
        if source.is_file():
            shutil.copy2(source, distribution / name)
    for name in ("docs", "examples"):
        source = internal / name
        if source.is_dir():
            shutil.copytree(source, distribution / name, dirs_exist_ok=True)


def _assert_bundled_references(distribution: Path) -> None:
    bundle = distribution / "_internal" / "aiops_diagnostics" / "_bundle"
    required = (
        ".env.example",
        "SOP.md",
        "充电桩问题排查SOP.md",
        "docs/architecture.md",
        "src/aiops_diagnostics/engine.py",
        "src/aiops_diagnostics/rules.py",
        "examples/fixtures/ocpp_consistent.json",
        "examples/fixtures/ykc_amount_mismatch.json",
        "examples/fixtures/missing_tx_data.json",
    )
    missing = [relative for relative in required if not (bundle / relative).is_file()]
    if missing:
        raise SystemExit(f"portable distribution is missing bundled references: {', '.join(missing)}")


def _assert_no_embedded_secrets(distribution: Path) -> None:
    forbidden_names = {"auth.json", "production.env"}
    for path in distribution.rglob("*"):
        if not path.is_file():
            continue
        if path.name in forbidden_names or path.suffix.lower() == ".key":
            raise SystemExit(f"portable distribution contains a forbidden secret file: {path}")


def _project_version(pyproject: Path) -> str:
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


if __name__ == "__main__":
    main()
