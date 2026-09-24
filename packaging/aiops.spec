from pathlib import Path

from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).resolve().parent

codex_datas, codex_binaries, codex_hiddenimports = collect_all("codex_cli_bin")
sdk_datas, sdk_binaries, sdk_hiddenimports = collect_all("openai_codex")

bundle_datas = [
    (str(project_root / ".env.example"), "aiops_diagnostics/_bundle"),
    (str(project_root / "SOP.md"), "aiops_diagnostics/_bundle"),
    (str(project_root / "充电桩问题排查SOP.md"), "aiops_diagnostics/_bundle"),
    (str(project_root / "docs/architecture.md"), "aiops_diagnostics/_bundle/docs"),
    (str(project_root / "docs/gateway.md"), "aiops_diagnostics/_bundle/docs"),
    (
        str(project_root / "src/aiops_diagnostics/engine.py"),
        "aiops_diagnostics/_bundle/src/aiops_diagnostics",
    ),
    (
        str(project_root / "src/aiops_diagnostics/rules.py"),
        "aiops_diagnostics/_bundle/src/aiops_diagnostics",
    ),
    (str(project_root / "examples/fixtures"), "aiops_diagnostics/_bundle/examples/fixtures"),
    (str(project_root / ".env.example"), "."),
    (str(project_root / "README.md"), "."),
    (str(project_root / "docs/gateway.md"), "docs"),
    (str(project_root / "examples/fixtures"), "examples/fixtures"),
]

analysis = Analysis(
    [str(project_root / "src/aiops_diagnostics/__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[*codex_binaries, *sdk_binaries],
    datas=[*codex_datas, *sdk_datas, *bundle_datas],
    hiddenimports=[*codex_hiddenimports, *sdk_hiddenimports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="aiops",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="aiops",
)
