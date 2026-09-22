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


def _package_modules() -> set[str]:
    return {path.stem for path in PACKAGE.glob("*.py")}


def _import_edges() -> dict[str, set[str]]:
    """Every in-package import edge, including deferred (in-function) imports."""
    modules = _package_modules()
    edges: dict[str, set[str]] = {module: set() for module in modules}
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if len(parts) > 1 and parts[0] == PACKAGE_NAME and parts[1] in modules:
                    edges[path.stem].add(parts[1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if len(parts) > 1 and parts[0] == PACKAGE_NAME and parts[1] in modules:
                        edges[path.stem].add(parts[1])
    return edges


def test_every_package_module_is_named_in_the_readme() -> None:
    """A new module that nobody documents is exactly the onboarding cost this README exists to remove."""
    readme = _readme()
    undocumented = sorted(
        module
        for module in _package_modules()
        if module != "__init__" and f"{module}.py" not in readme and f"`{module}`" not in readme
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


def test_package_import_graph_stays_acyclic() -> None:
    """The README tells the reader to trust the layer diagram; a cycle would invalidate it."""
    edges = _import_edges()
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)

    def visit(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for neighbor in sorted(edges.get(node, ())):
            if color.get(neighbor) == GRAY:
                return [*stack[stack.index(neighbor) :], neighbor]
            if color.get(neighbor) == WHITE:
                found = visit(neighbor, stack)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for module in sorted(edges):
        if color[module] == WHITE:
            cycle = visit(module, [])
            assert cycle is None, "包内出现导入环，README 的分层说明已失效: " + " -> ".join(cycle or [])
