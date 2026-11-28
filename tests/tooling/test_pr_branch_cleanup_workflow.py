"""闭环校验未合入关闭 PR 分支自动清理与 GitLab 镜像周期兜底薄壳工作流（ADR-0364 / fleet-kit@v1）。"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cleanup-closed-pr-branch.yml"
MIRROR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gitlab-mirror.yml"


def _load_yaml(path: Path) -> dict:
    # PyYAML 1.1 treats bare `on` key as boolean True.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_cleanup_workflow_shape_permissions_and_reusable_target() -> None:
    data = _load_yaml(CLEANUP_WORKFLOW)
    on_cfg = data.get("on", data.get(True))
    assert on_cfg == {"pull_request_target": {"types": ["closed"]}}

    job = data["jobs"]["delete-branch"]
    assert job["uses"] == "kilbertert/fleet-kit/.github/workflows/cleanup-closed-pr-branch.yml@v1"
    assert job["permissions"] == {"actions": "write", "contents": "write", "pull-requests": "read"}
    condition = " ".join(str(job["if"]).split())
    assert "github.event.pull_request.merged == false" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert job["with"]["trigger_gitlab_mirror"] is True


def test_gitlab_mirror_workflow_keeps_triggers_and_reusable_target() -> None:
    data = _load_yaml(MIRROR_WORKFLOW)
    on_cfg = data.get("on", data.get(True))
    assert "push" in on_cfg
    assert "delete" in on_cfg
    assert "workflow_dispatch" in on_cfg
    assert on_cfg["schedule"] == [{"cron": "15 3 * * 0"}]

    job = data["jobs"]["mirror"]
    assert job["uses"] == "kilbertert/fleet-kit/.github/workflows/gitlab-mirror.yml@v1"
    assert job["with"]["gitlab_target_url"] == "git.qushiyun.com:801/ai-products/ai-ops.git"
    assert job["secrets"] == "inherit"
