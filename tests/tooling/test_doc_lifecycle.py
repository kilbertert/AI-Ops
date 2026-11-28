"""验证文档自动减债生命周期引擎（ADR-0371）：写入门禁、幽灵引用、llmdoc 漂移与 GC 体检。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tools" / "verification" / "doc_lifecycle.py"

_SPEC = importlib.util.spec_from_file_location("doc_lifecycle", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
doc_lifecycle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(doc_lifecycle)


def _git(root: Path, *args: str, env_date: str | None = None) -> str:
    env = None
    if env_date:
        import os

        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = f"{env_date}T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = f"{env_date}T00:00:00Z"
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, env=env).strip()


@pytest.fixture
def sample_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.test")

    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "ops").mkdir()
    (repo / "ops/deploy.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    (repo / "docs/adr").mkdir(parents=True)
    (repo / "docs/agents").mkdir(parents=True)
    (repo / "docs/validation").mkdir(parents=True)
    (repo / "llmdoc").mkdir()

    policy = {
        "schema": "aiops.doc_lifecycle/v1",
        "archive_repo": "kilbertert/AI-Ops-history-archive",
        "archive_ref": "4bed22cbabbcbe80f242c83275a46d3f550f996d",
        "adr_high_water_mark": 10,
        "thresholds": {
            "tombstone_cooldown_days": 30,
            "unreachable_receipt_cooldown_days": 60,
            "llmdoc_drift_commits": 2,
            "bloat_tombstone_adrs": 1,
            "bloat_active_adrs": 10,
            "bloat_unreachable_receipts": 2,
        },
        "gc_roots": ["README.md", "docs/adr-index.md", "docs/agents/", "src/", "llmdoc/"],
        "protected_paths": ["docs/adr-index.md", "docs/validation/README.md"],
        "archived_adr_numbers": ["0003", "0005"],
    }
    (repo / "docs/doc-lifecycle.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n[index](docs/adr-index.md)\n", encoding="utf-8")
    (repo / "docs/adr-index.md").write_text(
        "# Index\n- [ADR-0001](adr/0001-core.md)\n- [ADR-0002](adr/0002-old-way.md)\n",
        encoding="utf-8",
    )
    (repo / "docs/adr/0001-core.md").write_text(
        "# 1. Core\n\n状态：已采纳\n\n## 备选方案\n- 方案 A：否决。\n",
        encoding="utf-8",
    )
    (repo / "docs/adr/0002-old-way.md").write_text(
        "---\nstatus: active\n---\n# 2. Old way\n\n## 备选方案\n- 方案 A：否决。\n",
        encoding="utf-8",
    )
    (repo / "docs/agents/runbook.md").write_text(
        "# Runbook\n运行 `ops/deploy.sh` 并核对 [回执](../validation/2026-01-01-linked.md)。\n",
        encoding="utf-8",
    )
    (repo / "docs/validation/README.md").write_text("# Validation\n", encoding="utf-8")
    (repo / "docs/validation/2026-01-01-linked.md").write_text("# Linked receipt\n", encoding="utf-8")
    (repo / "docs/validation/2026-01-02-orphan.md").write_text("# Orphan receipt\n", encoding="utf-8")
    (repo / "docs/validation/2026-01-03-held.md").write_text(
        "---\nhold: true\n---\n# Held receipt\n",
        encoding="utf-8",
    )
    (repo / "llmdoc/app.mdx").write_text(
        "---\ncode:\n  paths:\n    - src/app.py\n---\n# App\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: baseline", env_date="2026-01-10")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "llmdoc/meta.json").write_text(
        json.dumps({"documents": {"app.mdx": {"validatedRevision": base_sha}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "llmdoc/meta.json")
    _git(repo, "commit", "-m", "chore: record llmdoc baseline", env_date="2026-01-10")
    return repo


def test_check_pr_blocks_archived_adr_number_reuse_and_missing_index(sample_repo: Path) -> None:
    (sample_repo / "docs/adr/0003-reused.md").write_text(
        "---\nstatus: active\n---\n# 3. Reused\n\n## 备选方案\n- 无。\n",
        encoding="utf-8",
    )
    result = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert not result["passed"]
    codes = {e["code"] for e in result["errors"]}
    assert "reused_archived_adr_number" in codes
    assert "unindexed_active_adr" in codes


def test_check_pr_enforces_explicit_repeal_symmetry(sample_repo: Path) -> None:
    (sample_repo / "docs/adr/0011-new-way.md").write_text(
        '---\nstatus: active\nsupersedes: ["0002"]\n---\n# 11. New way\n\n## 备选方案\n- 保留旧法。\n',
        encoding="utf-8",
    )
    (sample_repo / "docs/adr-index.md").write_text(
        "# Index\n"
        "- [ADR-0001](adr/0001-core.md)\n"
        "- [ADR-0002](adr/0002-old-way.md)\n"
        "- [ADR-0011](adr/0011-new-way.md)\n",
        encoding="utf-8",
    )
    bad = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert not bad["passed"]
    assert any(e["code"] == "missing_explicit_repeal" for e in bad["errors"])

    # Updating target 0002 to superseded with superseded_by clears the gate
    (sample_repo / "docs/adr/0002-old-way.md").write_text(
        '---\nstatus: superseded\nsuperseded_by: "0011"\n---\n# 2. Old way\n\n## 备选方案\n- 方案 A。\n',
        encoding="utf-8",
    )
    good = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert good["passed"], good["errors"]


def test_check_pr_enforces_sunset_date_on_temporary_docs(sample_repo: Path) -> None:
    (sample_repo / "docs/adr/0012-temp-bridge.md").write_text(
        "---\nstatus: temporary\n---\n# 12. Temp\n\n## 备选方案\n- 无。\n",
        encoding="utf-8",
    )
    (sample_repo / "docs/adr-index.md").write_text(
        "# Index\n"
        "- [ADR-0001](adr/0001-core.md)\n"
        "- [ADR-0002](adr/0002-old-way.md)\n"
        "- [ADR-0012](adr/0012-temp-bridge.md)\n",
        encoding="utf-8",
    )
    bad = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert any(e["code"] == "missing_sunset_date" for e in bad["errors"])

    (sample_repo / "docs/adr/0012-temp-bridge.md").write_text(
        "---\nstatus: temporary\nexpires_after: 2026-09-01\n---\n# 12. Temp\n\n## 备选方案\n- 无。\n",
        encoding="utf-8",
    )
    good = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert good["passed"], good["errors"]


def test_check_pr_reports_ghost_path_references_and_broken_adr_links(sample_repo: Path) -> None:
    (sample_repo / "ops/deploy.sh").unlink()
    (sample_repo / "docs/agents/broken.md").write_text(
        "# Broken\n见 [missing](../adr/0999-missing.md)。\n",
        encoding="utf-8",
    )
    result = doc_lifecycle.check_pr(sample_repo, "HEAD", today=date(2026, 6, 1))
    assert not result["passed"]
    assert any(
        e["code"] == "broken_adr_link" and e["path"] == "docs/agents/broken.md" for e in result["errors"]
    )
    assert any(
        a["code"] == "ghost_path_reference"
        and a["path"] == "docs/agents/runbook.md"
        and "ops/deploy.sh" in a["deleted_sources"]
        for a in result["advisories"]
    )


def test_audit_detects_tombstones_sunset_drift_unreachable_and_respects_hold(sample_repo: Path) -> None:
    # Tombstoned ADR (>30d), expired temp doc, and 2 drift commits on src/app.py
    (sample_repo / "docs/adr/0002-old-way.md").write_text(
        '---\nstatus: superseded\nsuperseded_by: "0001"\n---\n# 2. Old way\n\n## 备选方案\n- 方案 A。\n',
        encoding="utf-8",
    )
    (sample_repo / "docs/agents/temp-guide.md").write_text(
        "---\n"
        "status: temporary\n"
        "expires_after: 2026-03-01\n"
        "last_reviewed: 2026-01-15\n"
        "review_cycle_days: 30\n"
        "---\n"
        "# Temp\n",
        encoding="utf-8",
    )
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-m", "docs: mark tombstone and temp guide", env_date="2026-02-01")

    for step in (1, 2):
        (sample_repo / "src/app.py").write_text(f"print({step})\n", encoding="utf-8")
        _git(sample_repo, "add", "src/app.py")
        _git(sample_repo, "commit", "-m", f"feat: change app {step}", env_date=f"2026-02-0{step + 2}")

    report = doc_lifecycle.audit_repo(sample_repo, today=date(2026, 6, 1))
    assert report["needs_compaction"] is True
    assert report["has_actionable_findings"] is True
    assert [i["path"] for i in report["tombstone_adrs_ready"]] == ["docs/adr/0002-old-way.md"]
    assert [i["path"] for i in report["expired_docs"]] == ["docs/agents/temp-guide.md"]
    assert [i["path"] for i in report["stale_reviews"]] == ["docs/agents/temp-guide.md"]
    assert len(report["llmdoc_drift"]) == 1 and report["llmdoc_drift"][0]["drift_commits"] == 2

    unreachable_paths = [i["path"] for i in report["unreachable_receipts_ready"]]
    assert "docs/validation/2026-01-02-orphan.md" in unreachable_paths
    assert "docs/validation/2026-01-01-linked.md" not in unreachable_paths
    assert "docs/validation/2026-01-03-held.md" not in unreachable_paths  # Legal Hold respected

    md = doc_lifecycle.render_audit_markdown(report)
    assert "<!-- aiops-doc-lifecycle-ledger -->" in md
    assert "doc_archive.py" in md


def test_parse_doc_metadata_does_not_misclassify_active_adr_that_supersedes_older_one() -> None:
    text = "# 348. main 集成与版本发布分开\n\n状态：已采纳；替代ADR0112的逐提交部署备注方式，更新ADR0233。\n"
    assert doc_lifecycle.parse_doc_metadata(text)["status"] == "active"

    partial = "# 340. Schema v4\n\nStatus: Accepted（第 2 条已由 [ADR-0343](...) 替代；第 1 条仍现行）\n"
    assert doc_lifecycle.parse_doc_metadata(partial)["status"] == "active"


def test_llmdoc_drift_falls_back_to_doc_last_commit_when_validated_revision_squashed(
    sample_repo: Path,
) -> None:
    (sample_repo / "llmdoc/meta.json").write_text(
        json.dumps({"documents": {"app.mdx": {"validatedRevision": "0" * 40}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(sample_repo, "add", "llmdoc/meta.json")
    _git(sample_repo, "commit", "-m", "chore: squashed rev", env_date="2026-01-12")
    for step in (1, 2):
        (sample_repo / "src/app.py").write_text(f"print('drift-{step}')\n", encoding="utf-8")
        _git(sample_repo, "add", "src/app.py")
        _git(sample_repo, "commit", "-m", f"feat: drift {step}", env_date=f"2026-01-1{step + 3}")
    report = doc_lifecycle.audit_repo(sample_repo, today=date(2026, 6, 1))
    assert len(report["llmdoc_drift"]) == 1
    assert report["llmdoc_drift"][0]["drift_commits"] == 2


def test_live_repository_has_zero_broken_adr_links_and_complete_index() -> None:
    report = doc_lifecycle.audit_repo(REPO_ROOT, today=date(2026, 11, 28))
    assert report["broken_adr_links"] == [], report["broken_adr_links"]
    assert report["unindexed_active_adrs"] == [], report["unindexed_active_adrs"]
    assert report["counts"]["tombstone_adrs_total"] == 0
    assert report["counts"]["active_adrs"] >= 126
