"""aiops-dify-edge 共享 OCI 镜像内部导入闭环与旧 tar/systemd 资产退役守卫（ADR-0367 / ADR-0369）。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE_DOCKERFILE = ROOT / "deploy/dify-36/edge/Dockerfile"


def _packaged_edge_modules() -> dict[str, Path]:
    """解析 edge Dockerfile COPY 指令实际打入 `/opt/aiops-edge` 的全部 Python 模块。"""
    source_roots = {
        "services/dify-console-bridge/dify_console_bridge": (
            "dify_console_bridge",
            ROOT / "services/dify-console-bridge/dify_console_bridge",
        ),
        "services/dify-login-proxy/dify_login_proxy": (
            "dify_login_proxy",
            ROOT / "services/dify-login-proxy/dify_login_proxy",
        ),
        "src/aiops_diagnostics": ("aiops_diagnostics", ROOT / "src/aiops_diagnostics"),
    }
    modules: dict[str, Path] = {}
    for line in EDGE_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) < 3 or parts[0] != "COPY" or parts[1].startswith("--from="):
            continue
        src_rel = parts[1].rstrip("/")
        src_path = ROOT / src_rel
        for prefix, (pkg, pkg_root) in source_roots.items():
            if src_rel != prefix and not src_rel.startswith(f"{prefix}/"):
                continue
            files = [src_path] if src_path.is_file() else sorted(src_path.rglob("*.py"))
            for py_file in files:
                if py_file.suffix != ".py":
                    continue
                rel_parts = py_file.relative_to(pkg_root).with_suffix("").parts
                if rel_parts[-1] == "__init__":
                    mod = ".".join((pkg, *rel_parts[:-1]))
                else:
                    mod = ".".join((pkg, *rel_parts))
                modules[mod] = py_file
    return modules


def _is_repo_module(qualified: str) -> bool:
    for root in (
        ROOT / "src",
        ROOT / "services/dify-console-bridge",
        ROOT / "services/dify-login-proxy",
    ):
        base = root / qualified.replace(".", "/")
        if base.with_suffix(".py").is_file() or (base / "__init__.py").is_file():
            return True
    return False


def _resolve_import_from(module: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if node.level > 0:
        parts = module.split(".")
        pkg_parts = parts if is_package else parts[:-1]
        up = node.level - 1
        if up > len(pkg_parts):
            return None
        base_parts = pkg_parts[: len(pkg_parts) - up] if up else list(pkg_parts)
        if node.module:
            base_parts.extend(node.module.split("."))
        return ".".join(base_parts) if base_parts else None
    return node.module


def test_edge_oci_image_imports_are_self_contained() -> None:
    """从 dify_console_bridge 与 dify_login_proxy 出发追踪可达导入，确保全部打入 edge OCI 镜像。"""
    packaged = _packaged_edge_modules()
    assert "dify_console_bridge.bridge" in packaged
    assert "dify_console_bridge.self_service_bridge" in packaged
    assert "dify_login_proxy.server" in packaged
    assert "dify_login_proxy.service" in packaged

    prefixes = ("aiops_diagnostics", "dify_console_bridge", "dify_login_proxy")
    visited: set[str] = set()
    queue = sorted(m for m in packaged if m.startswith(("dify_console_bridge.", "dify_login_proxy.")))
    offenders: list[str] = []

    while queue:
        module = queue.pop(0)
        if module in visited:
            continue
        visited.add(module)
        path = packaged[module]
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(module, is_package, node)
                if resolved and (
                    resolved in prefixes or resolved.startswith(tuple(f"{p}." for p in prefixes))
                ):
                    targets.append(resolved)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        candidate_sub = f"{resolved}.{alias.name}"
                        if _is_repo_module(candidate_sub):
                            targets.append(candidate_sub)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in prefixes or alias.name.startswith(tuple(f"{p}." for p in prefixes)):
                        targets.append(alias.name)
            for target in targets:
                if target not in packaged:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports unpackaged {target}")
                elif target not in visited:
                    queue.append(target)

    assert offenders == [], "edge OCI 镜像缺少运行时可达内部模块：\n" + "\n".join(offenders)


def test_legacy_edge_tar_and_systemd_assets_retired() -> None:
    """确认旧手工 tar 打包/部署脚本与 systemd unit 已按 ADR-0369 彻底删除。"""
    retired = (
        ROOT / "ops/dify-console-bridge/build-bundle.sh",
        ROOT / "ops/dify-console-bridge/deploy.sh",
        ROOT / "ops/dify-console-bridge/aiops-dify-console-bridge.service",
        ROOT / "ops/dify-login-proxy/aiops-dify-login-proxy.service",
    )
    for path in retired:
        assert not path.exists(), f"旧发版资产应已退役删除: {path.relative_to(ROOT)}"

    active_deploy = ROOT / "deploy/dify-36/edge/deploy-edge-image.sh"
    assert active_deploy.is_file()
