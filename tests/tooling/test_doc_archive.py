"""离线端到端夹具测试：验证一键冷归档工具 doc_archive.py（ADR-0371）。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_SCRIPT = REPO_ROOT / "tools" / "verification" / "doc_archive.py"
LIFECYCLE_SCRIPT = REPO_ROOT / "tools" / "verification" / "doc_lifecycle.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


doc_archive = _load("doc_archive", ARCHIVE_SCRIPT)
doc_lifecycle = _load("doc_lifecycle", LIFECYCLE_SCRIPT)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


@pytest.fixture
def paired_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """构造隔离的主仓工作树 + 本地 bare 归档仓。"""
    cfg = tmp_path / "empty-gitconfig"
    cfg.write_text("[user]\n\tname = Fixture\n\temail = fixture@example.test\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # 1) Create bare archive repo seeded with an initial commit on main
    archive_bare = tmp_path / "archive.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(archive_bare)], check=True, capture_output=True
    )
    seed = tmp_path / "archive-seed"
    subprocess.run(["git", "clone", str(archive_bare), str(seed)], check=True, capture_output=True)
    (seed / "docs/validation").mkdir(parents=True)
    (seed / "docs/validation/old.md").write_text("# Old\n", encoding="utf-8")
    (seed / "MANIFEST.sha256").write_text("", encoding="utf-8")
    (seed / "ARCHIVE_LIST.json").write_text(
        json.dumps(
            {
                "source_repo": "kilbertert/AI-Ops",
                "source_commit": "11111111",
                "archived_count": 1,
                "archived_bytes": 6,
                "batches": [{"label": "batch-1", "count": 1}],
                "archived_paths": ["docs/validation/old.md"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (seed / "README.md").write_text("# Archive Repo\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "chore: seed archive")
    _git(seed, "push", "origin", "main")

    # 2) Create main repo
    main_repo = tmp_path / "main"
    subprocess.run(["git", "init", "-b", "main", str(main_repo)], check=True, capture_output=True)
    (main_repo / "docs/adr").mkdir(parents=True)
    (main_repo / "docs/agents").mkdir(parents=True)
    (main_repo / "docs/validation").mkdir(parents=True)
    (main_repo / "tests").mkdir()

    policy = {
        "schema": "aiops.doc_lifecycle/v1",
        "archive_repo": "kilbertert/AI-Ops-history-archive",
        "archive_ref": "initial",
        "adr_high_water_mark": 20,
        "thresholds": {
            "tombstone_cooldown_days": 30,
            "unreachable_receipt_cooldown_days": 60,
            "llmdoc_drift_commits": 20,
            "bloat_tombstone_adrs": 12,
            "bloat_active_adrs": 160,
            "bloat_unreachable_receipts": 30,
        },
        "gc_roots": ["docs/adr-index.md", "docs/agents/"],
        "protected_paths": ["docs/adr-index.md", "docs/validation/README.md"],
        "archived_adr_numbers": ["0003"],
    }
    (main_repo / "docs/doc-lifecycle.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    (main_repo / "docs/adr-index.md").write_text(
        "# Index\n"
        "> 当前生效的 3 份核心架构决策\n\n"
        "| ADR | 摘要 |\n|---|---|\n"
        "| [ADR-0001](adr/0001-keep.md) | 保留 |\n"
        "| [ADR-0010](adr/0010-retire.md) | 已退役 |\n"
        "| [ADR-0011](adr/0011-test-pinned.md) | 测试引用 |\n",
        encoding="utf-8",
    )
    (main_repo / "docs/adr/0001-keep.md").write_text(
        "# 1. Keep\n\n状态：已采纳\n\n参见旧方案 [ADR-0010](0010-retire.md)。\n\n## 备选方案\n- 无。\n",
        encoding="utf-8",
    )
    (main_repo / "docs/adr/0010-retire.md").write_text(
        '---\nstatus: superseded\nsuperseded_by: "0001"\n---\n# 10. Retire\n\n## 备选方案\n- 无。\n',
        encoding="utf-8",
    )
    (main_repo / "docs/adr/0011-test-pinned.md").write_text(
        "# 11. Pinned by test\n\n状态：已采纳\n\n## 备选方案\n- 无。\n",
        encoding="utf-8",
    )
    (main_repo / "docs/agents/guide.md").write_text(
        "# Guide\n背景见 [ADR-0010](../adr/0010-retire.md) 与 `docs/adr/0010-retire.md`。\n",
        encoding="utf-8",
    )
    (main_repo / "docs/validation/README.md").write_text("# Validation\n", encoding="utf-8")
    (main_repo / "docs/validation/2026-01-01-old.md").write_text(
        "# Receipt\n见 [ADR-0010](../adr/0010-retire.md)。\n",
        encoding="utf-8",
    )
    (main_repo / "docs/validation/2026-01-02-held.md").write_text(
        "---\nhold: true\n---\n# Held\n",
        encoding="utf-8",
    )
    (main_repo / "tests/test_contract.py").write_text(
        'ADR = "docs/adr/0011-test-pinned.md"\n',
        encoding="utf-8",
    )
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-m", "chore: baseline")
    return main_repo, archive_bare


def test_dry_run_makes_zero_writes(paired_repos: tuple[Path, Path]) -> None:
    main_repo, archive_bare = paired_repos
    before_head = _git(archive_bare, "rev-parse", "main")
    plan = doc_archive.plan_archive(main_repo, ["docs/adr/0010-retire.md"])
    assert plan["mode"] == "dry-run"
    assert plan["candidate_count"] == 1
    assert plan["adr_numbers_to_freeze"] == ["0010"]
    assert "docs/agents/guide.md" in plan["files_to_relink"]
    assert (main_repo / "docs/adr/0010-retire.md").is_file()
    assert _git(archive_bare, "rev-parse", "main") == before_head


def test_guards_reject_hold_protected_and_test_referenced(paired_repos: tuple[Path, Path]) -> None:
    main_repo, _ = paired_repos
    with pytest.raises(ValueError, match="Legal Hold"):
        doc_archive.plan_archive(main_repo, ["docs/validation/2026-01-02-held.md"])
    with pytest.raises(ValueError, match="受保护路径"):
        doc_archive.plan_archive(main_repo, ["docs/validation/README.md"])
    with pytest.raises(ValueError, match="硬引用"):
        doc_archive.plan_archive(main_repo, ["docs/adr/0011-test-pinned.md"])


def test_apply_archives_with_sha256_relinks_updates_index_and_freezes_number(
    paired_repos: tuple[Path, Path], tmp_path: Path
) -> None:
    main_repo, archive_bare = paired_repos
    result = doc_archive.apply_archive(
        main_repo,
        ["docs/adr/0010-retire.md", "docs/validation/2026-01-01-old.md"],
        reason="Quarterly compaction",
        archive_repo_url=str(archive_bare),
        batch_label="test-batch-2",
        today=date(2026, 6, 1),
    )

    sha = result["archive_sha"]
    assert result["archived_count"] == 2
    assert result["frozen_adr_numbers"] == ["0010"]
    assert result["adr_index_updated"] is True
    assert not (main_repo / "docs/adr/0010-retire.md").exists()
    assert not (main_repo / "docs/validation/2026-01-01-old.md").exists()

    # Verify links rewritten to pinned archive SHA permalink
    permalink = f"https://github.com/kilbertert/AI-Ops-history-archive/blob/{sha}/docs/adr/0010-retire.md"
    assert permalink in (main_repo / "docs/adr/0001-keep.md").read_text(encoding="utf-8")
    guide = (main_repo / "docs/agents/guide.md").read_text(encoding="utf-8")
    assert permalink in guide
    assert "`docs/adr/0010-retire.md`" not in guide

    # Verify docs/adr-index.md and docs/doc-lifecycle.json synchronized
    index_text = (main_repo / "docs/adr-index.md").read_text(encoding="utf-8")
    assert "0010-retire.md" not in index_text
    assert "当前生效的 2 份核心架构决策" in index_text

    policy = json.loads((main_repo / "docs/doc-lifecycle.json").read_text(encoding="utf-8"))
    assert "0010" in policy["archived_adr_numbers"]
    assert policy["archive_ref"] == sha

    # Verify archive repo integrity via fresh clone + sha256sum -c
    verify_clone = tmp_path / "verify-archive"
    subprocess.run(["git", "clone", str(archive_bare), str(verify_clone)], check=True, capture_output=True)
    subprocess.run(["sha256sum", "-c", "MANIFEST.sha256", "--quiet"], cwd=verify_clone, check=True)
    arch_meta = json.loads((verify_clone / "ARCHIVE_LIST.json").read_text(encoding="utf-8"))
    assert len(arch_meta["batches"]) == 2
    assert arch_meta["batches"][1]["label"] == "test-batch-2"

    # Verify check-pr passes on main repo after archival
    pr_res = doc_lifecycle.check_pr(main_repo, "HEAD", today=date(2026, 6, 1))
    assert pr_res["passed"], pr_res["errors"]
