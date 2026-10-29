"""Shape guards for the architecture README.

The README makes two checkable promises about this repository:

1. it explains every module in the package (its stated purpose is to remove the
   cognitive cost of onboarding), and
2. it is a working map — its relative links resolve, and its claim that the
   package is acyclic holds.

These guards are deliberately about *shape*, not prose: they fail when the doc
drifts from the code, which is the only failure mode a reviewer would otherwise
have to notice by hand. They do not and cannot check whether the explanation is
any good.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

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
    """包括函数内延迟导入和from包import模块，不遗漏子包。"""
    modules = _package_modules()
    edges: dict[str, set[str]] = {module: set() for module in modules}
    prefix = PACKAGE_NAME + "."
    for path in PACKAGE.rglob("*.py"):
        source = _module_name(path)
        if source not in edges:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            candidates = []
            if isinstance(node, ast.ImportFrom) and node.module:
                candidates.append(node.module)
                candidates.extend(node.module + "." + alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            for target in candidates:
                if target.startswith(prefix) and target[len(prefix) :] in modules:
                    edges[source].add(target[len(prefix) :])
    return edges


def test_every_package_module_is_named_in_the_readme() -> None:
    """A new module that nobody documents is exactly the onboarding cost this README exists to remove."""
    readme = _readme()
    undocumented = sorted(
        module
        for module in _package_modules()
        if not (PACKAGE / module.replace(".", "/") / "__init__.py").exists()
        and f"{module}.py" not in readme
        and f"`{module}`" not in readme
    )
    assert undocumented == [], "README.md 未说明这些模块（请补进第三节的分层表）: " + ", ".join(undocumented)


def test_readme_module_references_point_at_real_files() -> None:
    """A renamed module must not leave the README naming a file that no longer exists.

    The predicate is deliberately "a file of this name exists", not "a package
    module of this name exists": the README must be free to cite test files
    (`test_readme_architecture.py`) and non-source artifacts without this guard
    mistaking a legitimate reference for a rotted one.
    """
    known = {path.stem for path in PROJECT_ROOT.rglob("*.py") if ".venv" not in path.parts}
    referenced = {match.group(1) for match in re.finditer(r"`([a-z_][a-z0-9_]*)\.py(?::\d+)?`", _readme())}
    unknown = sorted(name for name in referenced if name not in known)
    assert unknown == [], "README.md 引用了仓库中不存在的 .py 文件: " + ", ".join(unknown)


def test_readme_relative_links_resolve() -> None:
    """The README is the documentation hub; a rotted link silently hides the authoritative doc."""
    broken: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", _readme()):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (PROJECT_ROOT / path).exists():
            broken.append(target)
    assert broken == [], "README.md 中的相对链接不可解析: " + ", ".join(sorted(set(broken)))


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
    """已知装配循环按具体边冻结，不能用新增循环扩大旧边界。"""
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
    existing = {
        ("execution.agent_runs", "gateway_config"),
        ("gateway_config", "execution.chat_execution"),
        ("execution.chat_execution", "execution.agent_runs"),
        ("execution.chat_execution", "dify_chat_workflow"),
        ("execution.chat_execution", "gateway_config"),
        ("dify_chat_workflow", "gateway_config"),
    }
    assert not cyclic - existing, "新增包内循环边: " + repr(sorted(cyclic - existing))


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
    import pytest

    with pytest.raises(AssertionError, match="新增包内循环边"):
        test_package_import_graph_has_no_new_cycles()
