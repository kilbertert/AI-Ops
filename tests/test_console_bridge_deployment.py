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


def _bundled_bridge_modules() -> set[str]:
    """解析 build-bundle.sh 实际打包进发布桥的 aiops_diagnostics 模块集合。"""
    script = (ROOT / "ops/dify-console-bridge/build-bundle.sh").read_text(encoding="utf-8")
    modules: set[str] = set()
    for line in script.splitlines():
        token = line.strip().rstrip("\\").strip()
        if not token.startswith("src/aiops_diagnostics/"):
            continue
        rel = token.removeprefix("src/aiops_diagnostics/")
        match = re.fullmatch(r"(.*/)?\{([^}]+)\}\.py", rel)
        if match:
            prefix = (match.group(1) or "").replace("/", ".")
            for item in match.group(2).split(","):
                name = item.strip()
                modules.add(prefix.rstrip(".") if name == "__init__" else prefix + name)
        elif rel.endswith(".py"):
            stem = rel.removesuffix(".py").replace("/", ".")
            modules.add("" if stem == "__init__" else stem)
    return {f"aiops_diagnostics.{m}" if m else "aiops_diagnostics" for m in modules}


_LOGIN_PROXY_ONLY_DEFS: dict[str, set[str]] = {
    # console_environments.py 与 dify-login-proxy 共用；发布桥只调用顶层 credentials()，
    # LoginEnvironments / refused 仅在完整 wheel 的登录代理进程内运行。
    "aiops_diagnostics.dify.console_environments": {"LoginEnvironments", "refused"},
}


def _iter_bundle_checked_nodes(module: str, tree: ast.Module):
    """遍历发布桥实际会执行的 AST 节点（排除共用模块里仅供登录代理使用的定义）。"""
    skip = _LOGIN_PROXY_ONLY_DEFS.get(module, set())
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name in skip:
            continue
        yield from ast.walk(stmt)


def test_console_bridge_bundle_imports_are_self_contained() -> None:
    """拦截桥模块新增未打包的包内导入（含延迟导入），避免本地全绿、线上 ModuleNotFoundError。"""
    bundled = _bundled_bridge_modules()
    assert "aiops_diagnostics.dify.dify_console_publish" in bundled
    assert "aiops_diagnostics.dify.self_service_bridge" in bundled

    offenders: list[str] = []
    for module in sorted(bundled):
        rel = module.removeprefix("aiops_diagnostics").lstrip(".")
        if not rel:
            path = ROOT / "src/aiops_diagnostics/__init__.py"
        else:
            candidate = ROOT / "src/aiops_diagnostics" / (rel.replace(".", "/") + ".py")
            path = candidate if candidate.is_file() else candidate.with_suffix("") / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _iter_bundle_checked_nodes(module, tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("aiops_diagnostics"):
                    targets.append(node.module)
                    for alias in node.names:
                        candidate_sub = f"{node.module}.{alias.name}"
                        if (ROOT / "src" / (candidate_sub.replace(".", "/") + ".py")).is_file():
                            targets.append(candidate_sub)
            elif isinstance(node, ast.Import):
                targets.extend(
                    alias.name for alias in node.names if alias.name.startswith("aiops_diagnostics")
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
        "dify/__init__",
        "dify/dify_console_publish",
        "common/__init__",
        "common/async_job_lifecycle",
        "common/bounded_http",
        "common/http_auth",
        "common/order_reference",
        "identity/__init__",
        "identity/order_identity",
    ):
        (package / f"{name}.py").parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/aiops_diagnostics" / f"{name}.py", package / f"{name}.py")
    for name in ("dify-publish-button.js", "dify-console-publish.conf"):
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
    unit.write_text("ExecStart=python -m aiops_diagnostics.dify_console_publish\n")
    code = tmp_path / "installed"
    code.mkdir()
    (code / "old.py").write_text("old package\n")
    js, snippet, nginx = (tmp_path / n for n in ("button.js", "snippet.conf", "nginx"))
    js.write_text("old js\n")
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
        "/srv/aiops-console-assets/dify-publish-button.js": js,
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
        assert unit.read_text() == "ExecStart=python -m aiops_diagnostics.dify_console_publish\n"
        assert list(code.iterdir()) == [code / "old.py"]
        assert js.read_text() == "old js\n"
        assert snippet.read_text() == "old snippet\n"
    else:
        assert "aiops_diagnostics.dify.dify_console_publish" in unit.read_text()
        assert (code / "dify/dify_console_publish.py").read_bytes() == (
            package / "dify/dify_console_publish.py"
        ).read_bytes()
        assert js.read_bytes() == (bundle / "dify-publish-button.js").read_bytes()
        assert snippet.read_bytes() == (bundle / "dify-console-publish.conf").read_bytes()
    assert (tmp_path / "backup/aiops_diagnostics/old.py").read_text() == "old package\n"
