"""文档自动减债生命周期引擎：写入门禁（check-pr）与全仓 Autovacuum 体检（audit）。

遵循 ADR-0371：100% 确定性规则，不依赖模型判定，不修改 .sandcastle/adr.check.mjs，
只读分析工作树并输出结构化 JSON / Markdown 报告。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

ADR_FILENAME_RE = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
INLINE_STATUS_RE = re.compile(r"^(?:\*\*)?(?:Status|状态)(?:\*\*)?\s*[:：]\s*(.+)$", re.I)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ADR_INDEX_LINK_RE = re.compile(r"\(adr/(0\d{3}-[a-z0-9-]+\.md)\)")
DRAFT_TITLE_RE = re.compile(r"(?:draft|proposed|wip|草稿|提案|拟议)", re.I)

TOMBSTONE_STATUSES = frozenset({"superseded", "deprecated", "withdrawn", "retired"})
VALID_FRONTMATTER_STATUSES = frozenset(
    {
        "active",
        "accepted",
        "temporary",
        "draft",
        "proposed",
        "superseded",
        "deprecated",
        "withdrawn",
        "retired",
    }
)
GHOST_SOURCE_PREFIXES = ("src/", "services/", "deploy/", "ops/", "tools/")
ACTIVE_DOC_SCAN_PREFIXES = ("docs/", "deploy/", "ops/", "llmdoc/")
ACTIVE_DOC_ROOT_FILES = frozenset({"README.md", "AGENTS.md", "CLAUDE.md", "GLOSSARY.md"})


def _load_doc_impact():
    spec = importlib.util.spec_from_file_location(
        "doc_impact", Path(__file__).resolve().with_name("doc_impact.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DOC_IMPACT = _load_doc_impact()
path_matches = _DOC_IMPACT.path_matches


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def try_git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None


def load_policy(root: Path) -> dict[str, Any]:
    policy_path = root / "docs" / "doc-lifecycle.json"
    if not policy_path.is_file():
        raise ValueError(f"缺少文档生命周期策略配置: {policy_path}")
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "aiops.doc_lifecycle/v1":
        raise ValueError(f"无效的文档生命周期策略 schema: {policy_path}")
    return data


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        raw = value.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            try:
                return date.fromisoformat(raw)
            except ValueError:
                return None
    return None


def _normalize_adr_number(value: Any) -> str | None:
    text = str(value).strip()
    m = re.match(r"^(?:ADR[- ]?)?0*(\d{1,4})$", text, re.I)
    if not m:
        return None
    return f"{int(m.group(1)):04d}"


def _classify_inline_status(raw: str) -> str:
    lower = raw.strip().lower()
    if lower.startswith(("accepted", "active", "已采纳", "已接受", "采纳", "生效")):
        return "active"
    if any(
        k in lower
        for k in (
            "superseded by",
            "已被",
            "已取代",
            "被取代",
            "由 adr-",
            "由 [adr-",
            "由adr-",
        )
    ):
        return "superseded"
    if any(k in lower for k in ("deprecated", "已废止", "废弃", "已退役", "retired", "已反转", "reversed")):
        return "deprecated"
    if any(k in lower for k in ("withdrawn", "撤回")):
        return "withdrawn"
    if any(k in lower for k in ("temporary", "临时过渡")):
        return "temporary"
    if any(k in lower for k in ("draft", "proposed", "草稿", "提案")):
        return "draft"
    return "active"


def parse_doc_metadata(text: str) -> dict[str, Any]:
    """解析可选 YAML frontmatter 与既有中英文状态行；不强迫存量文档改写格式。"""
    clean = text.lstrip("\ufeff")
    lines = clean.splitlines()
    fm: dict[str, Any] = {}
    body_lines = lines
    if lines and lines[0].strip() == "---":
        try:
            end_idx = lines.index("---", 1)
        except ValueError:
            end_idx = -1
        if end_idx > 0:
            loaded = yaml.safe_load("\n".join(lines[1:end_idx]))
            if isinstance(loaded, dict):
                fm = loaded
            body_lines = lines[end_idx + 1 :]

    title = next((line.strip().lstrip("# ").strip() for line in body_lines if line.startswith("# ")), "")
    inline_status: str | None = None
    for line in body_lines[:30]:
        m = INLINE_STATUS_RE.match(line.strip())
        if m:
            inline_status = m.group(1).strip()
            break

    raw_fm_status = fm.get("status")
    if isinstance(raw_fm_status, str) and raw_fm_status.strip():
        fm_status_norm = raw_fm_status.strip().lower()
        if fm_status_norm in VALID_FRONTMATTER_STATUSES:
            status = (
                "active"
                if fm_status_norm == "accepted"
                else ("draft" if fm_status_norm == "proposed" else fm_status_norm)
            )
        else:
            status = _classify_inline_status(raw_fm_status)
    elif inline_status:
        status = _classify_inline_status(inline_status)
    elif DRAFT_TITLE_RE.search(title):
        status = "draft"
    else:
        status = "active"

    supersedes_raw = fm.get("supersedes") or []
    if isinstance(supersedes_raw, (str, int)):
        supersedes_raw = [supersedes_raw]
    supersedes = [n for item in supersedes_raw if (n := _normalize_adr_number(item)) is not None]

    superseded_by_raw = fm.get("superseded_by")
    superseded_by = _normalize_adr_number(superseded_by_raw) if superseded_by_raw is not None else None

    expires_after = _parse_iso_date(fm.get("expires_after"))
    last_reviewed = _parse_iso_date(fm.get("last_reviewed"))
    review_cycle_raw = fm.get("review_cycle_days")
    review_cycle_days = (
        int(review_cycle_raw) if isinstance(review_cycle_raw, int) and review_cycle_raw > 0 else None
    )

    return {
        "has_frontmatter": bool(fm),
        "title": title,
        "status": status,
        "raw_status": str(raw_fm_status or inline_status or ""),
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "expires_after": expires_after,
        "last_reviewed": last_reviewed,
        "review_cycle_days": review_cycle_days,
        "hold": bool(fm.get("hold", False)),
        "code_paths": (fm.get("code") or {}).get("paths", []) if isinstance(fm.get("code"), dict) else [],
    }


def _tracked_files(root: Path) -> list[str]:
    out = git(root, "ls-files", "-z")
    return [p for p in out.split("\0") if p and (root / p).is_file()]


def _visible_files(root: Path) -> list[str]:
    out = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p and (root / p).is_file()]


def _file_last_commit_date(root: Path, rel_path: str) -> date | None:
    out = try_git(root, "log", "-1", "--format=%cs", "--", rel_path)
    if not out:
        return None
    return _parse_iso_date(out.strip())


def _find_broken_adr_links(root: Path, rel_paths: list[str]) -> list[dict[str, str]]:
    broken: list[dict[str, str]] = []
    for rel in rel_paths:
        if not rel.endswith((".md", ".mdx")) or rel.startswith(".sandcastle/logs/"):
            continue
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in MD_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if "adr" in path_part or re.search(r"0\d{3}-", path_part):
                resolved = (path.parent / path_part).resolve()
                if not resolved.exists():
                    broken.append({"source": rel, "target": target})
    return broken


def _indexed_adr_filenames(root: Path) -> set[str]:
    index_path = root / "docs" / "adr-index.md"
    if not index_path.is_file():
        return set()
    return set(ADR_INDEX_LINK_RE.findall(index_path.read_text(encoding="utf-8", errors="ignore")))


def _pattern_exists_in_repo(root: Path, pattern: str) -> bool:
    clean = pattern.split("#", 1)[0].rstrip("/")
    if not clean:
        return False
    if any(ch in clean for ch in ("*", "?", "[")):
        return any(root.glob(clean))
    return (root / clean).exists()


def _llmdoc_drift_report(root: Path, drift_threshold: int) -> list[dict[str, Any]]:
    llmdoc_dir = root / "llmdoc"
    meta_path = llmdoc_dir / "meta.json"
    if not llmdoc_dir.is_dir():
        return []
    meta_docs: dict[str, Any] = {}
    if meta_path.is_file():
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("documents"), dict):
            meta_docs = loaded["documents"]

    results: list[dict[str, Any]] = []
    for doc in sorted(llmdoc_dir.rglob("*.mdx")):
        rel_full = doc.relative_to(root).as_posix()
        rel_in_llmdoc = doc.relative_to(llmdoc_dir).as_posix()
        metadata = parse_doc_metadata(doc.read_text(encoding="utf-8", errors="ignore"))
        patterns = [p for p in metadata["code_paths"] if isinstance(p, str) and p.strip()]
        missing_paths = sorted(p for p in patterns if not _pattern_exists_in_repo(root, p))

        entry = meta_docs.get(rel_in_llmdoc, {})
        validated_rev = entry.get("validatedRevision") if isinstance(entry, dict) else None
        effective_rev = None
        if validated_rev and try_git(root, "cat-file", "-e", f"{validated_rev}^{{commit}}") is not None:
            effective_rev = validated_rev
        else:
            fallback = try_git(root, "log", "-1", "--format=%H", "--", rel_full)
            if fallback and fallback.strip():
                effective_rev = fallback.strip()

        drift_commits = 0
        changed_sources: list[str] = []
        if effective_rev:
            diff_out = (
                try_git(root, "diff", "--name-only", "--no-renames", "-z", f"{effective_rev}..HEAD") or ""
            )
            changed = [p for p in diff_out.split("\0") if p]
            matched = sorted({p for p in changed for pat in patterns if path_matches(p, pat)})
            changed_sources = matched
            if matched:
                log_out = try_git(root, "log", "--format=%H", f"{effective_rev}..HEAD", "--", *matched) or ""
                drift_commits = len([line for line in log_out.splitlines() if line.strip()])

        if missing_paths or drift_commits >= drift_threshold:
            results.append(
                {
                    "document": rel_full,
                    "validated_revision": validated_rev,
                    "drift_commits": drift_commits,
                    "changed_source_count": len(changed_sources),
                    "missing_code_paths": missing_paths,
                }
            )
    return results


def _collect_gc_root_files(root: Path, gc_roots: list[str], active_adr_paths: list[str]) -> list[str]:
    files: set[str] = set(active_adr_paths)
    tracked = set(_tracked_files(root))
    for entry in gc_roots:
        if entry.endswith("/"):
            for rel in tracked:
                if rel.startswith(entry):
                    files.add(rel)
        elif entry in tracked:
            files.add(entry)
    return sorted(files)


def _reachable_validation_receipts(root: Path, gc_root_files: list[str]) -> set[str]:
    """从 GC Roots 出发，经相对 Markdown 链接传递闭包计算可达的 docs/validation/*.md。"""
    reachable: set[str] = set()
    queue: deque[str] = deque()

    # Seed: scan all GC root files for direct links or literal mentions of docs/validation/*.md
    val_mention_re = re.compile(r"(?:docs/)?validation/([A-Za-z0-9._-]+\.md)")
    for rel in gc_root_files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in MD_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (path.parent / path_part).resolve()
            try:
                rel_target = resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            if (
                rel_target.startswith("docs/validation/")
                and rel_target.endswith(".md")
                and rel_target not in reachable
            ):
                reachable.add(rel_target)
                queue.append(rel_target)
        for fname in val_mention_re.findall(text):
            rel_target = f"docs/validation/{fname}"
            if (root / rel_target).is_file() and rel_target not in reachable:
                reachable.add(rel_target)
                queue.append(rel_target)

    # Transitive closure among validation receipts themselves
    while queue:
        current = queue.popleft()
        path = root / current
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in MD_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (path.parent / path_part).resolve()
            try:
                rel_target = resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            if (
                rel_target.startswith("docs/validation/")
                and rel_target.endswith(".md")
                and rel_target not in reachable
            ):
                reachable.add(rel_target)
                queue.append(rel_target)
    return reachable


def check_pr(root: Path, base: str, today: date | None = None) -> dict[str, Any]:
    """PR 级写入控制与代码-文档耦合提示。"""
    today = today or datetime.now(UTC).date()
    policy = load_policy(root)
    archived_numbers = set(policy.get("archived_adr_numbers", []))

    requested = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    merge_base = git(root, "merge-base", requested, "HEAD").strip()

    diff_out = git(root, "diff", "--name-status", "-z", merge_base)
    tokens = [t for t in diff_out.split("\0") if t]
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        st = tokens[i]
        if st.startswith(("R", "C")):
            old_p, new_p = tokens[i + 1], tokens[i + 2]
            if st.startswith("R"):
                renamed.append((old_p, new_p))
            else:
                added.append(new_p)
            i += 3
        else:
            p = tokens[i + 1]
            if st == "A":
                added.append(p)
            elif st == "D":
                deleted.append(p)
            else:
                modified.append(p)
            i += 2

    # Include untracked non-ignored files in working tree
    untracked = [
        p
        for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        if p and (root / p).is_file()
    ]
    added_set = set(added) | set(untracked) | {new for _, new in renamed}
    touched_existing = sorted((added_set | set(modified)) - set(deleted))

    errors: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    # 1) ADR numbering watermark + archive reuse + duplicate checks
    adr_dir = root / "docs" / "adr"
    current_adrs_by_num: dict[str, list[str]] = {}
    if adr_dir.is_dir():
        for p in sorted(adr_dir.glob("*.md")):
            m = ADR_FILENAME_RE.match(p.name)
            if m:
                current_adrs_by_num.setdefault(m.group(1), []).append(p.name)

    indexed_adrs = _indexed_adr_filenames(root)

    for rel in sorted(added_set):
        if rel.startswith("docs/adr/") and rel.endswith(".md"):
            fname = Path(rel).name
            m = ADR_FILENAME_RE.match(fname)
            if not m:
                continue
            num = m.group(1)
            if num in archived_numbers:
                next_num = int(policy["adr_high_water_mark"]) + 1
                errors.append(
                    {
                        "code": "reused_archived_adr_number",
                        "path": rel,
                        "message": (
                            f"ADR 编号 {num} 已在归档仓占用，严禁复用；请从水位线 {next_num:04d}+ 顺延分配"
                        ),
                    }
                )
            if len(current_adrs_by_num.get(num, [])) > 1:
                errors.append(
                    {
                        "code": "duplicate_adr_number",
                        "path": rel,
                        "message": f"ADR 编号 {num} 冲突: {current_adrs_by_num[num]}",
                    }
                )

            p = root / rel
            if p.is_file():
                meta = parse_doc_metadata(p.read_text(encoding="utf-8", errors="ignore"))
                if (
                    meta["status"] not in TOMBSTONE_STATUSES
                    and meta["status"] != "draft"
                    and fname not in indexed_adrs
                ):
                    errors.append(
                        {
                            "code": "unindexed_active_adr",
                            "path": rel,
                            "message": f"新增现行 ADR `{fname}` 未登记至 `docs/adr-index.md`",
                        }
                    )

    # 2) Explicit repeal symmetry + temporary sunset requirement + overdue touched docs
    for rel in touched_existing:
        if not rel.endswith((".md", ".mdx")):
            continue
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        meta = parse_doc_metadata(text)

        if meta["status"] == "temporary" and meta["expires_after"] is None:
            errors.append(
                {
                    "code": "missing_sunset_date",
                    "path": rel,
                    "message": (
                        "文档声明了 `status: temporary`，但缺少合法的日落条款 `expires_after: YYYY-MM-DD`"
                    ),
                }
            )

        if rel.startswith("docs/adr/") and meta["supersedes"]:
            src_match = ADR_FILENAME_RE.match(p.name)
            src_num = src_match.group(1) if src_match else None
            for target_num in meta["supersedes"]:
                targets = current_adrs_by_num.get(target_num, [])
                for target_name in targets:
                    target_path = adr_dir / target_name
                    target_meta = parse_doc_metadata(target_path.read_text(encoding="utf-8", errors="ignore"))
                    target_is_superseded = target_meta["status"] == "superseded"
                    links_back = src_num is not None and (
                        target_meta["superseded_by"] == src_num
                        or src_num in target_meta["raw_status"]
                        or str(int(src_num)) in target_meta["raw_status"]
                    )
                    if not target_is_superseded or (src_num and not links_back):
                        errors.append(
                            {
                                "code": "missing_explicit_repeal",
                                "path": rel,
                                "target": f"docs/adr/{target_name}",
                                "message": (
                                    f"`{p.name}` 声明了 `supersedes: [{target_num}]`，"
                                    f"但未在同一 PR 中将 `docs/adr/{target_name}` 标记为 "
                                    f'`status: superseded` 并写明 `superseded_by: "{src_num}"`'
                                ),
                            }
                        )

        if not meta["hold"]:
            if meta["expires_after"] is not None and meta["expires_after"] < today:
                advisories.append(
                    {
                        "code": "sunset_expired",
                        "path": rel,
                        "message": f"临时文档已过日落日期 {meta['expires_after'].isoformat()}",
                    }
                )
            if meta["last_reviewed"] is not None and meta["review_cycle_days"] is not None:
                age = (today - meta["last_reviewed"]).days
                if age > meta["review_cycle_days"]:
                    advisories.append(
                        {
                            "code": "review_overdue",
                            "path": rel,
                            "message": (
                                f"上次复核 {meta['last_reviewed'].isoformat()} 距今 {age} 天"
                                f"（周期 {meta['review_cycle_days']} 天）"
                            ),
                        }
                    )

    # 3) Broken relative ADR links in touched markdown files
    for item in _find_broken_adr_links(root, touched_existing):
        errors.append(
            {
                "code": "broken_adr_link",
                "path": item["source"],
                "target": item["target"],
                "message": f"`{item['source']}` 包含不可解析的 ADR 相对链接 `{item['target']}`",
            }
        )

    # 4) Ghost references to deleted or renamed code/script paths
    gone_sources = sorted(
        {
            p
            for p in (deleted + [old for old, _ in renamed])
            if p.startswith(GHOST_SOURCE_PREFIXES) and not (root / p).exists()
        }
    )
    if gone_sources:
        visible = _visible_files(root)
        doc_candidates = [
            rel
            for rel in visible
            if rel.endswith((".md", ".mdx"))
            and not rel.startswith(("docs/validation/", "docs/adr/", ".sandcastle/logs/"))
            and (rel in ACTIVE_DOC_ROOT_FILES or rel.startswith(ACTIVE_DOC_SCAN_PREFIXES))
        ]
        for rel in doc_candidates:
            content = (root / rel).read_text(encoding="utf-8", errors="ignore")
            hits = [src for src in gone_sources if src in content]
            if hits:
                advisories.append(
                    {
                        "code": "ghost_path_reference",
                        "path": rel,
                        "deleted_sources": hits,
                        "message": f"现役文档仍引用本 PR 已删除/改名的路径: {', '.join(hits)}",
                    }
                )

    # 5) llmdoc impact & missing code.paths advisory
    llmdoc_impact: dict[str, Any] | None = None
    if (root / "llmdoc").is_dir() and any((root / "llmdoc").rglob("*.mdx")):
        try:
            llmdoc_impact = _DOC_IMPACT.report(root, base)
        except (subprocess.CalledProcessError, ValueError, OSError, yaml.YAMLError):
            llmdoc_impact = None
        if llmdoc_impact and llmdoc_impact.get("affected"):
            affected_count = len(llmdoc_impact["affected"])
            advisories.append(
                {
                    "code": "llmdoc_affected",
                    "affected": llmdoc_impact["affected"],
                    "message": (
                        f"本次改动涉及 {affected_count} 篇 `llmdoc` 文档映射路径，合入前请核对是否需同步更新"
                    ),
                }
            )
        for doc in sorted((root / "llmdoc").rglob("*.mdx")):
            meta = parse_doc_metadata(doc.read_text(encoding="utf-8", errors="ignore"))
            missing = sorted(
                p for p in meta["code_paths"] if isinstance(p, str) and not _pattern_exists_in_repo(root, p)
            )
            if missing:
                doc_rel = doc.relative_to(root).as_posix()
                advisories.append(
                    {
                        "code": "llmdoc_missing_code_path",
                        "path": doc_rel,
                        "missing_paths": missing,
                        "message": f"`{doc_rel}` 的 `code.paths` 引用了不存在的路径: {', '.join(missing)}",
                    }
                )

    return {
        "mode": "check-pr",
        "base_sha": merge_base,
        "head_sha": git(root, "rev-parse", "HEAD").strip(),
        "today": today.isoformat(),
        "passed": len(errors) == 0,
        "errors": errors,
        "advisories": advisories,
        "changed_summary": {
            "added": len(added_set),
            "modified": len(modified),
            "deleted": len(deleted),
            "renamed": len(renamed),
        },
    }


def audit_repo(root: Path, today: date | None = None) -> dict[str, Any]:
    """全仓确定性 Autovacuum 体检：墓碑、日落、漂移、GC 不可达回执与膨胀水位。"""
    today = today or datetime.now(UTC).date()
    policy = load_policy(root)
    thresholds = policy["thresholds"]
    protected = set(policy.get("protected_paths", []))

    tracked = _tracked_files(root)
    adr_dir = root / "docs" / "adr"
    adr_files = sorted(p for p in adr_dir.glob("*.md")) if adr_dir.is_dir() else []
    indexed_adrs = _indexed_adr_filenames(root)

    active_adr_paths: list[str] = []
    tombstone_adrs_all: list[dict[str, Any]] = []
    tombstone_adrs_ready: list[dict[str, Any]] = []
    unindexed_active_adrs: list[str] = []

    for p in adr_files:
        rel = p.relative_to(root).as_posix()
        meta = parse_doc_metadata(p.read_text(encoding="utf-8", errors="ignore"))
        if meta["status"] in TOMBSTONE_STATUSES:
            last_dt = _file_last_commit_date(root, rel)
            age_days = (today - last_dt).days if last_dt else 0
            rec = {
                "path": rel,
                "status": meta["status"],
                "title": meta["title"],
                "last_commit": last_dt.isoformat() if last_dt else None,
                "age_days": age_days,
                "hold": meta["hold"],
            }
            tombstone_adrs_all.append(rec)
            if not meta["hold"] and age_days >= int(thresholds["tombstone_cooldown_days"]):
                tombstone_adrs_ready.append(rec)
        else:
            active_adr_paths.append(rel)
            if meta["status"] != "draft" and p.name not in indexed_adrs:
                unindexed_active_adrs.append(rel)

    # Scan all tracked markdown for expires_after & last_reviewed
    expired_docs: list[dict[str, Any]] = []
    stale_reviews: list[dict[str, Any]] = []
    for rel in tracked:
        if not rel.endswith((".md", ".mdx")) or rel.startswith(".sandcastle/logs/"):
            continue
        meta = parse_doc_metadata((root / rel).read_text(encoding="utf-8", errors="ignore"))
        if meta["hold"]:
            continue
        if meta["expires_after"] is not None and meta["expires_after"] < today:
            expired_docs.append(
                {
                    "path": rel,
                    "expires_after": meta["expires_after"].isoformat(),
                    "overdue_days": (today - meta["expires_after"]).days,
                    "title": meta["title"],
                }
            )
        if meta["last_reviewed"] is not None and meta["review_cycle_days"] is not None:
            age = (today - meta["last_reviewed"]).days
            if age > meta["review_cycle_days"]:
                stale_reviews.append(
                    {
                        "path": rel,
                        "last_reviewed": meta["last_reviewed"].isoformat(),
                        "review_cycle_days": meta["review_cycle_days"],
                        "age_days": age,
                    }
                )

    llmdoc_drift = _llmdoc_drift_report(root, int(thresholds["llmdoc_drift_commits"]))

    # GC reachability for docs/validation/*.md
    gc_root_files = _collect_gc_root_files(root, policy.get("gc_roots", []), active_adr_paths)
    reachable_receipts = _reachable_validation_receipts(root, gc_root_files)
    unreachable_receipts_all: list[dict[str, Any]] = []
    unreachable_receipts_ready: list[dict[str, Any]] = []
    val_dir = root / "docs" / "validation"
    if val_dir.is_dir():
        for p in sorted(val_dir.glob("*.md")):
            rel = p.relative_to(root).as_posix()
            if rel in protected or rel in reachable_receipts:
                continue
            meta = parse_doc_metadata(p.read_text(encoding="utf-8", errors="ignore"))
            if meta["hold"]:
                continue
            last_dt = _file_last_commit_date(root, rel)
            age_days = (today - last_dt).days if last_dt else 0
            rec = {
                "path": rel,
                "last_commit": last_dt.isoformat() if last_dt else None,
                "age_days": age_days,
            }
            unreachable_receipts_all.append(rec)
            if age_days >= int(thresholds["unreachable_receipt_cooldown_days"]):
                unreachable_receipts_ready.append(rec)

    broken_adr_links = _find_broken_adr_links(root, tracked)

    bloat_reasons: list[str] = []
    if len(tombstone_adrs_ready) >= int(thresholds["bloat_tombstone_adrs"]):
        tomb_limit = thresholds["bloat_tombstone_adrs"]
        bloat_reasons.append(f"墓碑 ADR 冷静期满数 ({len(tombstone_adrs_ready)}) >= 阈值 ({tomb_limit})")
    if len(active_adr_paths) >= int(thresholds["bloat_active_adrs"]):
        active_limit = thresholds["bloat_active_adrs"]
        bloat_reasons.append(f"主仓现行 ADR 总数 ({len(active_adr_paths)}) >= 阈值 ({active_limit})")
    if len(unreachable_receipts_ready) >= int(thresholds["bloat_unreachable_receipts"]):
        rec_limit = thresholds["bloat_unreachable_receipts"]
        bloat_reasons.append(f"GC 不可达旧回执数 ({len(unreachable_receipts_ready)}) >= 阈值 ({rec_limit})")
    if expired_docs:
        bloat_reasons.append(f"存在 {len(expired_docs)} 份已过日落期（expires_after）的临时文档")

    archive_candidates = sorted(
        {item["path"] for item in tombstone_adrs_ready}
        | {item["path"] for item in unreachable_receipts_ready}
    )
    has_actionable_findings = bool(
        bloat_reasons
        or tombstone_adrs_ready
        or expired_docs
        or stale_reviews
        or llmdoc_drift
        or unreachable_receipts_ready
        or broken_adr_links
        or unindexed_active_adrs
    )

    return {
        "mode": "audit",
        "head_sha": git(root, "rev-parse", "HEAD").strip(),
        "today": today.isoformat(),
        "counts": {
            "active_adrs": len(active_adr_paths),
            "tombstone_adrs_total": len(tombstone_adrs_all),
            "tombstone_adrs_ready": len(tombstone_adrs_ready),
            "expired_docs": len(expired_docs),
            "stale_reviews": len(stale_reviews),
            "llmdoc_drift": len(llmdoc_drift),
            "unreachable_receipts_total": len(unreachable_receipts_all),
            "unreachable_receipts_ready": len(unreachable_receipts_ready),
            "broken_adr_links": len(broken_adr_links),
            "unindexed_active_adrs": len(unindexed_active_adrs),
        },
        "needs_compaction": bool(bloat_reasons),
        "has_actionable_findings": has_actionable_findings,
        "bloat_reasons": bloat_reasons,
        "tombstone_adrs_ready": tombstone_adrs_ready,
        "expired_docs": expired_docs,
        "stale_reviews": stale_reviews,
        "llmdoc_drift": llmdoc_drift,
        "unreachable_receipts_ready": unreachable_receipts_ready,
        "broken_adr_links": broken_adr_links,
        "unindexed_active_adrs": unindexed_active_adrs,
        "archive_candidates": archive_candidates,
    }


def render_pr_markdown(data: dict[str, Any]) -> str:
    lines = ["### 文档生命周期检查（Doc-GC `check-pr`）", ""]
    status_label = "通过" if data["passed"] else "存在阻断错误"
    lines.append(
        f"- **结果**：{status_label}（阻断项 `{len(data['errors'])}`，提醒项 `{len(data['advisories'])}`）"
    )
    lines.append(f"- **范围**：`{data['base_sha'][:8]}..{data['head_sha'][:8]}`")
    if data["errors"]:
        lines.extend(["", "#### 阻断项（须在合入前修复）"])
        for err in data["errors"]:
            lines.append(f"- `{err['code']}` — {err['message']}")
    if data["advisories"]:
        lines.extend(["", "#### 失鲜与耦合提醒（非阻断）"])
        for adv in data["advisories"]:
            lines.append(f"- `{adv['code']}` — {adv['message']}")
    if not data["errors"] and not data["advisories"]:
        lines.append("- 本次改动未发现文档门禁问题或代码-文档失鲜提醒。")
    return "\n".join(lines) + "\n"


def render_audit_markdown(data: dict[str, Any]) -> str:
    c = data["counts"]
    lines = [
        "<!-- aiops-doc-lifecycle-ledger -->",
        f"## 文档 Autovacuum 周期体检报告（{data['today']}）",
        "",
        f"- **扫描修订**：`{data['head_sha'][:8]}`",
        f"- **是否触发压实阈值**：`{'是' if data['needs_compaction'] else '否'}`",
        "",
        "| 指标 | 当前数量 |",
        "|---|---|",
        f"| 主仓现行 ADR | {c['active_adrs']} |",
        f"| 墓碑 ADR（已过冷静期 / 全部） | {c['tombstone_adrs_ready']} / {c['tombstone_adrs_total']} |",
        f"| 日落到期临时文档 | {c['expired_docs']} |",
        f"| 复核超期文档 | {c['stale_reviews']} |",
        f"| `llmdoc` 强漂移/断路径文档 | {c['llmdoc_drift']} |",
        (
            f"| GC 不可达旧回执（已过冷静期 / 全部） | "
            f"{c['unreachable_receipts_ready']} / {c['unreachable_receipts_total']} |"
        ),
        f"| ADR 相对断链 / 未入索引现行 ADR | {c['broken_adr_links']} / {c['unindexed_active_adrs']} |",
    ]

    if data["bloat_reasons"]:
        lines.extend(["", "### 触发压实阈值的原因"])
        for r in data["bloat_reasons"]:
            lines.append(f"- {r}")

    if data["tombstone_adrs_ready"]:
        lines.extend(["", "### 可归档墓碑 ADR（冷静期满）"])
        for item in data["tombstone_adrs_ready"]:
            lines.append(f"- `{item['path']}`（`{item['status']}`，已静置 {item['age_days']} 天）")

    if data["expired_docs"]:
        lines.extend(["", "### 已过日落期（`expires_after`）的临时文档"])
        for item in data["expired_docs"]:
            lines.append(
                f"- `{item['path']}`（到期日 {item['expires_after']}，超期 {item['overdue_days']} 天）"
            )

    if data["llmdoc_drift"]:
        lines.extend(["", "### `llmdoc` 漂移 / `code.paths` 缺失告警"])
        for item in data["llmdoc_drift"]:
            extra = (
                f"，缺失路径：{', '.join(f'`{p}`' for p in item['missing_code_paths'])}"
                if item["missing_code_paths"]
                else ""
            )
            lines.append(f"- `{item['document']}`（关联提交漂移 {item['drift_commits']} 次{extra}）")

    if data["unreachable_receipts_ready"]:
        lines.extend(["", "### GC 不可达旧验收回执（冷静期满）"])
        for item in data["unreachable_receipts_ready"][:50]:
            lines.append(f"- `{item['path']}`（已静置 {item['age_days']} 天）")
        if len(data["unreachable_receipts_ready"]) > 50:
            lines.append(
                f"- … 其余 {len(data['unreachable_receipts_ready']) - 50} 份请通过 `--format json` 查看"
            )

    if data["broken_adr_links"]:
        lines.extend(["", "### 全仓 ADR 断链"])
        for item in data["broken_adr_links"]:
            lines.append(f"- `{item['source']}` → `{item['target']}`")

    if data["unindexed_active_adrs"]:
        lines.extend(["", "### 未收录至 `docs/adr-index.md` 的现行 ADR"])
        for p in data["unindexed_active_adrs"]:
            lines.append(f"- `{p}`")

    if data["archive_candidates"]:
        lines.extend(
            [
                "",
                "### 建议的一键归档操作（经人工复核后执行）",
                "```bash",
                "cat <<'EOF' > /tmp/doc-archive-candidates.txt",
                *data["archive_candidates"],
                "EOF",
                "uv run python tools/verification/doc_archive.py \\",
                '  --paths-file /tmp/doc-archive-candidates.txt --reason "周期性文档减债归档" --dry-run',
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pr = sub.add_parser("check-pr", help="PR 级写入门禁与失鲜提醒")
    p_pr.add_argument("--base", required=True, help="基线提交或引用，例如 origin/main")
    p_pr.add_argument("--today", help="覆盖当前日期 YYYY-MM-DD（仅用于确定性复测）")
    p_pr.add_argument("--format", choices=("json", "markdown"), default="markdown")
    p_pr.add_argument("--fail-on-error", action="store_true", help="存在阻断错误时返回退出码 1")

    p_audit = sub.add_parser("audit", help="全仓 Autovacuum 体检")
    p_audit.add_argument("--today", help="覆盖当前日期 YYYY-MM-DD（仅用于确定性复测）")
    p_audit.add_argument("--format", choices=("json", "markdown"), default="markdown")

    args = parser.parse_args(argv)
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        today_val = _parse_iso_date(args.today) if args.today else None
        if args.today and today_val is None:
            raise ValueError(f"无效日期格式: {args.today}")

        if args.command == "check-pr":
            result = check_pr(root, args.base, today=today_val)
            output = (
                json.dumps(result, ensure_ascii=False, indent=2)
                if args.format == "json"
                else render_pr_markdown(result)
            )
            print(output, end="" if output.endswith("\n") else "\n")
            if args.fail_on_error and not result["passed"]:
                return 1
            return 0

        result = audit_repo(root, today=today_val)
        output = (
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.format == "json"
            else render_audit_markdown(result)
        )
        print(output, end="" if output.endswith("\n") else "\n")
        return 0
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"文档生命周期检查失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
