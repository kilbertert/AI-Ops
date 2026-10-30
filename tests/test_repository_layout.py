"""已归位目录的新增文件门禁；职责合理性仍由审查判断。"""

import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 每项分别列允许的根层文件和一级目录；改变边界须同步目录规范。
LAYOUT = {
    "": (
        frozenset(
            [
                ".afk-bootstrap.json",
                ".dev-host.toml",
                ".env.example",
                ".gitignore",
                "AGENTS.md",
                "CLAUDE.md",
                "GLOSSARY.md",
                "README.md",
                "RELEASE-NOTES.md",
                "SOP.md",
                "VERSION",
                "acceptance.feature",
                "package-lock.json",
                "package.json",
                "pnpm-workspace.yaml",
                "pyproject.toml",
                "qa-plan.md",
                "uv.lock",
                "充电桩问题排查SOP.md",
            ]
        ),
        frozenset(
            [
                ".claude",
                ".github",
                ".sandcastle",
                ".spec",
                "deploy",
                "docs",
                "examples",
                "integrations",
                "ops",
                "plugins",
                "services",
                "src",
                "tests",
                "tools",
            ]
        ),
    ),
    "src": (
        frozenset(),
        frozenset(["aiops_diagnostics"]),
    ),
    "src/aiops_diagnostics": (
        frozenset(["__init__.py", "admin_cli.py", "gateway_server.py", "tdengine_proxy.py"]),
        frozenset(
            [
                "common",
                "consumer",
                "conversation",
                "data",
                "dify",
                "execution",
                "gateway",
                "health",
                "identity",
                "knowledge",
                "operator",
                "publication",
            ]
        ),
    ),
    "tools": (
        frozenset(["README.md", "test.sh"]),
        frozenset(["content", "experiments", "inventory", "verification"]),
    ),
    "tests": (
        frozenset(
            [
                "conftest.py",
                "milestone_archive_headings.json",
                "operator_native_preview.mjs",
                "operator_support.py",
                "test_bridge_knowledge_open.py",
                "test_cd_deploy_scripts.py",
                "test_console_bridge_deployment.py",
                "test_env_41_editable_guard.py",
                "test_env_example.py",
                "test_h5_assistant_routes.py",
                "test_inner_api_forwarder.py",
                "test_milestone_archive_is_frozen.py",
                "test_node_runtime_deploy.py",
                "test_parallel_setup.py",
                "test_readme_architecture.py",
                "test_release_version.py",
                "test_repository_layout.py",
                "test_runtime_retirement.py",
                "test_tdengine_proxy.py",
                "test_transport_convergence.py",
            ]
        ),
        frozenset(
            [
                "common",
                "consumer",
                "conversation",
                "data",
                "dify",
                "execution",
                "fixtures",
                "gateway",
                "health",
                "identity",
                "integrations",
                "knowledge",
                "operations",
                "publication",
                "tooling",
            ]
        ),
    ),
    "services": (
        frozenset(),
        frozenset(["execution-node", "model-proxy"]),
    ),
    "integrations": (
        frozenset(),
        frozenset(["cpms-java"]),
    ),
    "deploy": (
        frozenset(
            [
                "check-41-editable.sh",
                "classify-remote-result.sh",
                "company-gitlab-api.sh",
                "d4-cutover.py",
                "deploy-41.sh",
                "manual-deploys.md",
                "operator-h5.py",
                "record-manual-deploy.sh",
                "routing-window.sh",
                "test-rollback-classifier.sh",
            ]
        ),
        frozenset(["dify-36", "dify-chat", "kb-service", "m2-live-03", "node-runtime"]),
    ),
    "ops": (
        frozenset(
            [
                "README.md",
                "aiops-tdengine-readonly-proxy.service",
                "dify-exposure-registry.md",
                "h5-assistant-routes.md",
            ]
        ),
        frozenset(
            [
                "dify-console-bridge",
                "dify-inner-api",
                "dify-login-proxy",
                "environments",
                "knowledge-console",
                "nginx",
            ]
        ),
    ),
}


def misplaced(paths):
    bad = set()
    for path in paths:
        item = PurePosixPath(path)
        if item.parts[0] == "java":
            bad.add(path)
        for root, (files, directories) in LAYOUT.items():
            if not item.is_relative_to(root):
                continue
            parts = item.relative_to(root).parts
            if (
                not parts
                or (len(parts) == 1 and parts[0] not in files)
                or len(parts) > 1
                and parts[0] not in directories
            ):
                bad.add(path)
    return sorted(bad)


def visible_paths(root):
    result = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    )
    return [
        path
        for path in result.decode().split("\0")
        if path and ((root / path).exists() or (root / path).is_symlink())
    ]


def test_repository_files_follow_the_directory_rules():
    bad = misplaced(visible_paths(ROOT))
    assert not bad, (
        "文件放置违反 docs/architecture/repository-layout.md 的现行目录规则；"
        "请按职责归位，新增一级目录/根层入口须在同一PR说明并更新规范：\n" + "\n".join(bad)
    )


@pytest.mark.parametrize(
    "path",
    [
        "new_server.py",
        "new_layer/main.py",
        "src/misc.py",
        "src/another_package/main.py",
        "src/aiops_diagnostics/new_business.py",
        "src/aiops_diagnostics/catalog.json",
        "src/aiops_diagnostics/utils/helper.py",
        "tools/new_probe.py",
        "tools/legacy/probe.py",
        "tests/test_new_business.py",
        "tests/misc/test_business.py",
        "services/new-service/app.py",
        "integrations/new-platform/client.py",
        "deploy/new-task.sh",
        "ops/new-task.sh",
        "java/README.md",
    ],
)
def test_new_root_files_and_undeclared_directories_are_rejected(path):
    assert misplaced([path]) == [path]


def test_existing_directories_allow_new_modules_tests_and_resources():
    paths = [
        "src/aiops_diagnostics/consumer/new_feature.py",
        "src/aiops_diagnostics/knowledge/templates/new.html",
        "tests/consumer/test_new_feature.py",
        "tools/verification/new_probe.py",
        "deploy/node-runtime/new-check.sh",
        "ops/nginx/new-site.conf",
        "services/execution-node/src/main/resources/new.json",
        "integrations/cpms-java/new-patch.java",
    ]
    assert misplaced(paths) == []


def test_git_inventory_includes_unstaged_files_and_ignores_build_cache(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("tools/cache/\n")
    (tmp_path / "tools/cache").mkdir(parents=True)
    (tmp_path / "tools/cache/generated.py").write_text("")
    (tmp_path / "tools/new_probe.py").write_text("")
    (tmp_path / "tools/new_link.py").symlink_to("missing.py")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    assert misplaced(visible_paths(tmp_path)) == ["tools/new_link.py", "tools/new_probe.py"]
