import sys
from pathlib import Path

from aiops_diagnostics.platform_paths import reference_root


def test_frozen_reference_root_wins_over_source_checkout(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "frozen" / "aiops_diagnostics" / "_bundle"
    bundle.mkdir(parents=True)
    source = tmp_path / "source"
    (source / "src" / "aiops_diagnostics").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='decoy'\n", encoding="utf-8")
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)

    assert reference_root() == bundle
