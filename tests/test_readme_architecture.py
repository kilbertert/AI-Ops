"""职责入口、链接和导入边界检查；不把文档措辞或长度当作业务正确性。"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

_PRUNED_DIRS = frozenset(
    {
        ".venv",
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
    }
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
PACKAGE = PROJECT_ROOT / "src" / "aiops_diagnostics"
PACKAGE_NAME = "aiops_diagnostics"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE).with_suffix("")
    return ".".join(relative.parts[:-1] if relative.name == "__init__" else relative.parts)


def _package_modules() -> set[str]:
    return {_module_name(path) for path in PACKAGE.rglob("*.py")} - {""}


def _import_edges() -> dict[str, set[str]]:
    """包括函数内延迟导入、from包import模块与相对导入，覆盖整个包依赖图。"""
    modules = _package_modules()
    edges: dict[str, set[str]] = {module: set() for module in modules}
    prefix = PACKAGE_NAME + "."
    for path in PACKAGE.rglob("*.py"):
        source = _module_name(path)
        if source not in edges:
            continue
        is_pkg = path.name == "__init__.py"
        pkg_parts = source.split(".") if is_pkg else source.split(".")[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            candidates = []
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    up = node.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up else list(pkg_parts)
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                    if base:
                        # `from . import sibling` inside a package `__init__.py` resolves `base` to the
                        # package itself; only add `base` when an explicit module name was given or it
                        # points outside the current package `__init__`.
                        if node.module or not is_pkg or base != source:
                            candidates.append(prefix + base)
                        candidates.extend(prefix + base + "." + alias.name for alias in node.names)
                    else:
                        candidates.extend(prefix + alias.name for alias in node.names)
                elif node.module:
                    candidates.append(node.module)
                    candidates.extend(node.module + "." + alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            for target in candidates:
                if target.startswith(prefix):
                    target_mod = target[len(prefix) :]
                    if target_mod in modules:
                        edges[source].add(target_mod)
    return edges


def test_every_package_area_has_a_readme_entry() -> None:
    """新职责不能失去入口；同职责新增模块不要求手工更新共享README。"""
    targets = set(re.findall(r"\[[^\]]*\]\((src/aiops_diagnostics/[^)#]+)\)", _readme()))
    areas = {path.parent for path in PACKAGE.rglob("*.py") if path.parent != PACKAGE}
    top_level = {path.relative_to(PACKAGE).parts[0] for path in areas}
    missing = sorted(area for area in top_level if f"src/aiops_diagnostics/{area}/" not in targets)
    assert not missing, "README缺少职责目录入口: " + ", ".join(missing)


def test_new_area_requires_a_link_but_new_module_does_not(tmp_path, monkeypatch):
    package = tmp_path / "src/aiops_diagnostics"
    (package / "existing").mkdir(parents=True)
    (package / "existing/new.py").write_text("")
    readme = tmp_path / "README.md"
    readme.write_text("[已有职责](src/aiops_diagnostics/existing/)\n")
    monkeypatch.setitem(globals(), "PACKAGE", package)
    monkeypatch.setitem(globals(), "README", readme)
    test_every_package_area_has_a_readme_entry()
    (package / "new_area").mkdir()
    (package / "new_area/mod.py").write_text("")
    with pytest.raises(AssertionError, match="new_area"):
        test_every_package_area_has_a_readme_entry()


def test_readme_module_references_point_at_real_files() -> None:
    """A renamed module must not leave the README naming a file that no longer exists.

    The predicate is deliberately "a file of this name exists", not "a package
    module of this name exists": the README must be free to cite test files
    (`test_readme_architecture.py`) and non-source artifacts without this guard
    mistaking a legitimate reference for a rotted one.
    """
    known: set[str] = set()
    for _dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [name for name in dirnames if name not in _PRUNED_DIRS]
        for filename in filenames:
            if filename.endswith(".py"):
                known.add(filename[:-3])
    referenced = {match.group(1) for match in re.finditer(r"`([a-z_][a-z0-9_]*)\.py(?::\d+)?`", _readme())}
    unknown = sorted(name for name in referenced if name not in known)
    assert unknown == [], "README.md 引用了仓库中不存在的 .py 文件: " + ", ".join(unknown)


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/README.md",
        "docs/adr-index.md",
        "docs/agents/current-delivery-state.md",
        "docs/agents/environment-registry.md",
    ],
)
def test_readme_relative_links_resolve(relative) -> None:
    """The README is the documentation hub; a rotted link silently hides the authoritative doc."""
    broken: list[str] = []
    document = PROJECT_ROOT / relative
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", document.read_text()):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (document.parent / path).exists():
            broken.append(target)
    assert broken == [], f"{relative} 中的相对链接不可解析: " + ", ".join(sorted(set(broken)))


def _github_anchor(heading: str) -> str:
    """GitHub's anchor rule: strip markup/punctuation, spaces become hyphens.

    Kept deliberately close to GitHub's own transform for the shapes this README
    uses (CJK headings, punctuation, parenthesised asides). It is not a general
    reimplementation — the point is to catch a TOC entry whose heading was
    renamed and whose anchor was not.
    """
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "", text)
    return text.strip().lower().replace(" ", "-")


def test_readme_toc_anchors_resolve() -> None:
    """Every in-page TOC link must point at a heading that still exists.

    The relative-link guard above deliberately skips `#anchors`, so renaming a
    heading leaves its table-of-contents entry pointing at an anchor nothing
    defines — the entry silently stops jumping and CI stays green. That is not
    hypothetical: shortening `## 二、设计哲学：…` to `## 二、设计哲学` did exactly
    this, and only a human reading the diff (via a review comment) caught it.
    """
    readme = _readme()
    headings = {_github_anchor(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", readme, re.M)}
    broken = [target for target in re.findall(r"\[[^\]]*\]\(#([^)]+)\)", readme) if target not in headings]
    assert broken == [], "README.md 的页内目录锚点没有对应标题（标题改名后锚点未同步？）: " + ", ".join(
        sorted(set(broken))
    )


def test_package_import_graph_has_no_new_cycles() -> None:
    """全包导入图（含绝对导入、相对导入与函数内延迟导入）必须保持严格无环（ADR-0339）。"""
    edges = _import_edges()

    def reachable(start):
        found = set()
        pending = list(edges[start])
        while pending:
            item = pending.pop()
            if item not in found:
                found.add(item)
                pending.extend(edges.get(item, ()))
        return found

    cyclic = {(a, b) for a, targets in edges.items() for b in targets if a in reachable(b)}
    assert not cyclic, "新增包内循环边: " + repr(sorted(cyclic))


def test_import_scan_includes_nested_modules_and_package_imports(tmp_path, monkeypatch):
    package = tmp_path / "sample"
    (package / "area").mkdir(parents=True)
    (package / "area/__init__.py").write_text("")
    (package / "area/one.py").write_text("from aiops_diagnostics.area import two\n")
    (package / "area/two.py").write_text("import aiops_diagnostics.area.one\n")
    monkeypatch.setitem(globals(), "PACKAGE", package)
    edges = _import_edges()
    assert "area.two" in edges["area.one"]
    assert "area.one" in edges["area.two"]
    with pytest.raises(AssertionError, match="新增包内循环边"):
        test_package_import_graph_has_no_new_cycles()


def test_import_scan_catches_relative_import_cycles(tmp_path, monkeypatch):
    package = tmp_path / "sample"
    (package / "area").mkdir(parents=True)
    (package / "area/__init__.py").write_text("")
    (package / "area/one.py").write_text("from . import two\n")
    (package / "area/two.py").write_text("from .one import something\n")
    monkeypatch.setitem(globals(), "PACKAGE", package)
    edges = _import_edges()
    assert "area.two" in edges["area.one"]
    assert "area.one" in edges["area.two"]
    with pytest.raises(AssertionError, match="新增包内循环边"):
        test_package_import_graph_has_no_new_cycles()


def test_import_scan_catches_explicit_self_imports(tmp_path, monkeypatch):
    package = tmp_path / "sample"
    (package / "area").mkdir(parents=True)
    (package / "area/__init__.py").write_text("from . import one\n")
    (package / "area/one.py").write_text("import aiops_diagnostics.area.one\n")
    monkeypatch.setitem(globals(), "PACKAGE", package)
    edges = _import_edges()
    assert "area" not in edges["area"]
    assert "area.one" in edges["area.one"]
    with pytest.raises(AssertionError, match="新增包内循环边"):
        test_package_import_graph_has_no_new_cycles()
