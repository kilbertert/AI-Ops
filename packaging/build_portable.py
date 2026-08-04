from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    _assert_no_embedded_secrets(distribution)
    version = _project_version(project_root / "pyproject.toml")
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64")
    archive_base = project_root / "dist" / f"aiops-diagnostics-{version}-{system}-{machine}"
    archive = Path(str(archive_base) + ".zip")
    if archive.exists():
        archive.unlink()
    shutil.make_archive(str(archive_base), "zip", distribution.parent, distribution.name)
    _smoke_test_archive(archive)
    print(f"portable archive: {archive}")


def _run(command: list[str], environment: dict[str, str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _smoke_test_archive(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="aiops-portable-acceptance-") as temporary:
        temporary_root = Path(temporary)
        extraction_root = temporary_root / "extracted"
        shutil.unpack_archive(archive, extraction_root)
        distribution = extraction_root / "aiops"
        _restore_archive_modes(archive, extraction_root)
        _assert_bundled_references(distribution)
        _assert_no_embedded_secrets(distribution)
        _smoke_test_distribution(distribution, temporary_root)


def _restore_archive_modes(archive: Path, extraction_root: Path) -> None:
    if os.name == "nt":
        return
    with zipfile.ZipFile(archive) as handle:
        executable_entries = {
            entry.filename: entry.external_attr >> 16
            for entry in handle.infolist()
            if not entry.is_dir() and (entry.external_attr >> 16) & 0o111
        }
    if "aiops/aiops" not in executable_entries:
        raise SystemExit("Linux portable archive did not preserve the executable bit")
    for relative, archived_mode in executable_entries.items():
        os.chmod(extraction_root.joinpath(*relative.split("/")), stat.S_IMODE(archived_mode))


def _smoke_test_distribution(distribution: Path, temporary_root: Path) -> None:
    executable = distribution / ("aiops.exe" if os.name == "nt" else "aiops")
    if not executable.is_file():
        raise SystemExit("extracted portable distribution is missing the executable")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("AIOPS_")}
    home = temporary_root / "home"
    environment.update(
        {
            "AIOPS_HOME": str(home),
            "AIOPS_GATEWAY_TOKEN_STORE": "file",
            "PATH": _minimal_path(environment),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    _run([str(executable), "--help"], environment, distribution)
    _run([str(executable), "remote", "--help"], environment, distribution)
    _run([str(executable), "init"], environment, distribution)
    _run([str(executable), "paths"], environment, distribution)
    _smoke_test_gateway_client(executable, environment, distribution, temporary_root)
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
        distribution / "_internal" / "codex_cli_bin" / "bin" / ("codex.exe" if os.name == "nt" else "codex")
    )
    _run(
        [str(executable), "__codex-launcher", str(bundled_codex), "--version"],
        environment,
        distribution,
    )
    fixture_cases = (
        ("ocpp_consistent.json", "订单 TEST-OCPP-0003 金额是否正常"),
        ("ykc_amount_mismatch.json", "订单 TEST-YKC-0001 金额是否正常"),
        ("missing_tx_data.json", "订单 TEST-MISSING-0002 为什么异常"),
    )
    for fixture_name, problem in fixture_cases:
        fixture = distribution / "examples" / "fixtures" / fixture_name
        _run(
            [str(executable), "diagnose", problem, "--fixture", str(fixture), "--json"],
            environment,
            distribution,
        )
    _validate_smoke_paths(home)
    print(f"portable OpenSSH client: {shutil.which('ssh', path=environment['PATH']) or 'not found'}")


def _minimal_path(environment: dict[str, str]) -> str:
    if os.name != "nt":
        return "/usr/bin:/bin"
    system_root = Path(environment.get("SYSTEMROOT") or environment.get("WINDIR") or "C:/Windows")
    return os.pathsep.join((str(system_root / "System32"), str(system_root / "System32/OpenSSH")))


def _smoke_test_gateway_client(
    executable: Path,
    environment: dict[str, str],
    distribution: Path,
    temporary_root: Path,
) -> None:
    enrollment_code = temporary_root / "gateway-enrollment.code"
    enrollment_code.write_text("portable-smoke-enrollment-code\n", encoding="utf-8")
    with _gateway_smoke_server() as gateway_url:
        _run(
            [
                str(executable),
                "remote",
                "enroll",
                "--url",
                gateway_url,
                "--profile",
                "portable-smoke",
                "--device-name",
                "portable-smoke",
                "--code-file",
                str(enrollment_code),
            ],
            environment,
            distribution,
        )
        _run(
            [str(executable), "remote", "doctor", "--profile", "portable-smoke"],
            environment,
            distribution,
        )
        _run(
            [str(executable), "remote", "runs", "--profile", "portable-smoke"],
            environment,
            distribution,
        )


@contextmanager
def _gateway_smoke_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewaySmokeHandler)
    thread = threading.Thread(target=server.serve_forever, name="gateway-smoke", daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _GatewaySmokeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._respond(
                {
                    "ok": True,
                    "service": "aiops-gateway-smoke",
                    "api_version": "v1",
                    "business_mutations": "disabled",
                }
            )
            return
        if self.path.startswith("/v1/runs") and self.headers.get("Authorization") == "Bearer smoke-token":
            self._respond({"runs": []})
            return
        self._respond({"detail": "request rejected"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/enroll":
            self._respond({"detail": "request rejected"}, status=404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if payload.get("code") != "portable-smoke-enrollment-code":
            self._respond({"detail": "invalid enrollment code"}, status=400)
            return
        self._respond(
            {
                "device_id": "dev_portable_smoke",
                "workspace_id": "portable-smoke",
                "tenant_id": None,
                "token": "smoke-token",
            },
            status=201,
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _respond(self, payload: dict[str, object], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _validate_smoke_paths(home: Path) -> None:
    from aiops_diagnostics.private_files import validate_private_directory, validate_private_file

    for directory in (
        home,
        home / "keys",
        home / "codex-home",
        home / "runs",
        home / "gateway-profiles",
        home / "gateway-tokens",
    ):
        validate_private_directory(directory)
    for file in (
        home / "production.env",
        home / "keys" / "smoke.key",
        home / "codex-home" / "config.toml",
        home / "gateway-profiles" / "portable-smoke.json",
        home / "gateway-tokens" / "portable-smoke.token",
    ):
        validate_private_file(file)


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
        "docs/gateway.md",
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
