"""实际文件替换与失败恢复；systemd/nginx 为本地命令替身，不接触服务宿主。"""

import ast
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bundled_bridge_modules() -> dict[str, Path]:
    """解析 build-bundle.sh 实际打包进发布桥的全部模块（独立桥包 + 最小 aiops_diagnostics 原语）。"""
    script = (ROOT / "ops/dify-console-bridge/build-bundle.sh").read_text(encoding="utf-8")
    modules: dict[str, Path] = {}
    roots = (
        ("services/dify-console-bridge/dify_console_bridge/", "dify_console_bridge"),
        ("src/aiops_diagnostics/", "aiops_diagnostics"),
    )
    for line in script.splitlines():
        token = line.strip().rstrip("\\").strip()
        for prefix_dir, pkg_name in roots:
            if not token.startswith(prefix_dir):
                continue
            rel = token.removeprefix(prefix_dir)
            base_dir = ROOT / prefix_dir
            match = re.fullmatch(r"(.*/)?\{([^}]+)\}\.py", rel)
            if match:
                sub_dir = match.group(1) or ""
                dot_prefix = sub_dir.replace("/", ".")
                for item in match.group(2).split(","):
                    name = item.strip()
                    mod = (
                        f"{pkg_name}.{dot_prefix.rstrip('.')}"
                        if name == "__init__" and dot_prefix
                        else (pkg_name if name == "__init__" else f"{pkg_name}.{dot_prefix}{name}")
                    )
                    modules[mod] = base_dir / sub_dir / f"{name}.py"
            elif rel.endswith(".py"):
                stem = rel.removesuffix(".py").replace("/", ".")
                mod = pkg_name if stem == "__init__" else f"{pkg_name}.{stem}"
                modules[mod] = base_dir / rel
    return modules


def _is_package_member(qualified: str) -> bool:
    """判断全限定模块名是否指向 src/ 或 dify-console-bridge 下的真实模块文件或子包目录。"""
    for root in (ROOT / "src", ROOT / "services/dify-console-bridge"):
        base = root / qualified.replace(".", "/")
        if base.with_suffix(".py").is_file() or (base / "__init__.py").is_file():
            return True
    return False


def _resolve_import_from(module: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    """解析绝对与相对 `from ... import ...` 的目标包/模块全限定名。"""
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


def test_console_bridge_bundle_imports_are_self_contained() -> None:
    """拦截桥模块新增未打包的包内导入（含延迟、相对与子包导入），防线上 ModuleNotFoundError。"""
    bundled = _bundled_bridge_modules()
    assert "dify_console_bridge.bridge" in bundled
    assert "dify_console_bridge.config" in bundled
    assert "dify_console_bridge.client" in bundled
    assert "dify_console_bridge.workflows" in bundled
    assert "dify_console_bridge.self_service_bridge" in bundled
    assert "dify_console_bridge.environments" in bundled
    assert not any(mod.startswith("aiops_diagnostics.dify") for mod in bundled)

    offenders: list[str] = []
    for module, path in sorted(bundled.items()):
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(module, is_package, node)
                if resolved and (
                    resolved in {"aiops_diagnostics", "dify_console_bridge"}
                    or resolved.startswith(("aiops_diagnostics.", "dify_console_bridge."))
                ):
                    targets.append(resolved)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        candidate_sub = f"{resolved}.{alias.name}"
                        if _is_package_member(candidate_sub):
                            targets.append(candidate_sub)
            elif isinstance(node, ast.Import):
                targets.extend(
                    alias.name
                    for alias in node.names
                    if alias.name in {"aiops_diagnostics", "dify_console_bridge"}
                    or alias.name.startswith(("aiops_diagnostics.", "dify_console_bridge."))
                )
            for target in targets:
                if target not in bundled:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports unbundled {target}")
    assert offenders == [], "发布桥子集包存在未随包交付的包内导入：\n" + "\n".join(offenders)


@pytest.mark.parametrize("failure", ["none", "reload", "transient_route", "permanent_route"])
def test_bridge_install_waits_for_new_routes_or_restores_all_files(tmp_path, failure):
    fail_reload = failure in {"reload", "permanent_route"}
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    package = bundle / "aiops_diagnostics"
    package.mkdir()
    for name in (
        "__init__",
        "common/__init__",
        "common/async_job_lifecycle",
        "common/bounded_http",
        "common/http_auth",
        "common/order_reference",
        "common/platform_constants",
        "common/private_files",
        "common/endpoint_policy",
        "gateway/__init__",
        "gateway/deployment_environment",
        "identity/__init__",
        "identity/order_identity",
    ):
        (package / f"{name}.py").parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/aiops_diagnostics" / f"{name}.py", package / f"{name}.py")
    bridge_pkg = bundle / "dify_console_bridge"
    bridge_pkg.mkdir()
    for name in (
        "__init__",
        "__main__",
        "bridge",
        "config",
        "client",
        "workflows",
        "self_service_bridge",
        "environments",
    ):
        shutil.copyfile(
            ROOT / "services/dify-console-bridge/dify_console_bridge" / f"{name}.py",
            bridge_pkg / f"{name}.py",
        )
    for name in ("dify-console-publish.conf",):
        shutil.copyfile(ROOT / "ops/dify-console-bridge" / name, bundle / name)
    (bundle / "REVISION").write_text("test-revision\n")
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle)}\n"
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        )
    )
    unit = tmp_path / "bridge.service"
    unit.write_text("ExecStart=python -m aiops_diagnostics.dify.dify_console_publish\n")
    code = tmp_path / "installed_aiops"
    code.mkdir()
    (code / "old.py").write_text("old package\n")
    bridge_installed = tmp_path / "installed_bridge"
    snippet, nginx = (tmp_path / n for n in ("snippet.conf", "nginx"))
    snippet.write_text("old snippet\n")
    nginx.write_text(
        '#!/bin/sh\nif [ "$*" = "-s reload" ] && [ -f "$FAIL_RELOAD" ]; then rm "$FAIL_RELOAD"; exit 1; fi\n'
    )
    nginx.chmod(0o700)
    if failure == "reload":
        (tmp_path / "fail").touch()
    if failure == "transient_route":
        (tmp_path / "old-route").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "systemctl": "exit 0",
        "sleep": "exit 0",
        "curl": 'if [ "$ROUTE_FAILURE" = permanent_route ] || [ -f "$OLD_ROUTE" ]; then '
        'rm -f "$OLD_ROUTE"; printf 404; exit 0; fi; '
        'case "$*" in *operator-preview*) preview=yes;; *) preview=no;; esac; '
        'while [ "$1" != "-o" ]; do shift; done; '
        'if [ "$preview" = yes ]; then printf \'{"code":"OPERATOR_PREVIEW_REJECTED","message":"refused"}\'; '
        'else printf \'{"published":false,"action":"","reason":"refused"}\'; fi > "$2"; printf 403',
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
    script = (ROOT / "ops/dify-console-bridge/deploy.sh").read_text()
    for before, after in {
        "/opt/aiops-console-bridge/aiops_diagnostics": code,
        "/opt/aiops-console-bridge/dify_console_bridge": bridge_installed,
        "/www/server/panel/vhost/nginx/snippets/dify-console-publish.conf": snippet,
        "/www/server/nginx/sbin/nginx": nginx,
        "/etc/systemd/system/aiops-dify-console-bridge.service": unit,
    }.items():
        script = script.replace(before, str(after))
    result = subprocess.run(
        ["bash", "-c", script, "deploy.sh", str(bundle), str(tmp_path / "backup")],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAIL_RELOAD": str(tmp_path / "fail"),
            "OLD_ROUTE": str(tmp_path / "old-route"),
            "ROUTE_FAILURE": failure,
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode != 0) == fail_reload, result.stdout + result.stderr
    if fail_reload:
        assert unit.read_text() == "ExecStart=python -m aiops_diagnostics.dify.dify_console_publish\n"
        assert list(code.iterdir()) == [code / "old.py"]
        assert not bridge_installed.exists()
        assert snippet.read_text() == "old snippet\n"
    else:
        assert " -m dify_console_bridge\n" in unit.read_text()
        assert (bridge_installed / "bridge.py").read_bytes() == (bridge_pkg / "bridge.py").read_bytes()
        assert snippet.read_bytes() == (bundle / "dify-console-publish.conf").read_bytes()
    assert (tmp_path / "backup/aiops_diagnostics/old.py").read_text() == "old package\n"
