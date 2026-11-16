"""独立Git夹具验证影响选择；来源变化不是文档错误的断言。"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "doc_impact", Path(__file__).resolve().parents[2] / "tools/verification/doc_impact.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report = module.report


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for args in [
        ("init",),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.test"),
    ]:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/old.py").write_text("old\n")
    (tmp_path / "llmdoc").mkdir()
    (tmp_path / "llmdoc/area.mdx").write_text(
        "---\ncode:\n  paths: [src/old.py#symbol, docs/current.md]\n---\n"
    )
    (tmp_path / "llmdoc/other.mdx").write_text("---\ncode:\n  paths: [other/]\n---\n")
    (tmp_path / "llmdoc/meta.json").write_text('{"validatedRevision":"unchanged"}\n')
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "test: baseline"], check=True, capture_output=True
    )
    return tmp_path


def test_rename_new_paths_and_unmapped_changes_do_not_write_state(repository):
    root = repository
    (root / "src/old.py").rename(root / "src/new.py")
    (root / "other").mkdir()
    (root / "other/new.md").write_text("new\n")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    result = report(root, "HEAD")
    assert result["affected"] == [
        {"document": "llmdoc/area.mdx", "changed_sources": ["src/old.py"]},
        {"document": "llmdoc/other.mdx", "changed_sources": ["other/new.md"]},
    ]
    assert result["unmapped"] == ["src/new.py"]
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_clean_tree_and_bad_baseline_are_distinct(repository):
    assert report(repository, "HEAD")["affected"] == []
    assert report(repository, "HEAD")["unmapped"] == []
    with pytest.raises(subprocess.CalledProcessError):
        report(repository, "missing-baseline")


def test_changed_document_source_is_included_even_if_llmdocignore_excludes_it(repository):
    (repository / "docs").mkdir()
    (repository / "docs/current.md").write_text("changed contract\n")
    (repository / ".llmdocignore").write_text("docs/\n")
    result = report(repository, "HEAD")
    assert result["affected"] == [{"document": "llmdoc/area.mdx", "changed_sources": ["docs/current.md"]}]
    assert json.loads((repository / "llmdoc/meta.json").read_text())["validatedRevision"] == "unchanged"
