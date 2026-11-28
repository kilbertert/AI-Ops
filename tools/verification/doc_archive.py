"""一键文档冷归档工具（ADR-0371）：先验 SHA-256 迁入归档仓，再主仓删旧、改写 permalink 与刷新索引。

默认执行只读 `--dry-run`；仅在显式传入 `--apply` 时写入归档仓与当前主仓工作树。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

HARD_CODE_REF_PREFIXES = ("src/", "tests/", "services/", ".sandcastle/")
ADR_PATH_RE = re.compile(r"^docs/adr/(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
MD_LINK_RE = re.compile(r"(\[[^\]]*\])\(([^)]+)\)")
BACKTICK_ADR_RE = re.compile(r"`docs/adr/(0\d{3}-[a-z0-9-]+\.md)`")


def _load_doc_lifecycle():
    spec = importlib.util.spec_from_file_location(
        "doc_lifecycle", Path(__file__).resolve().with_name("doc_lifecycle.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LC = _load_doc_lifecycle()
git = _LC.git
load_policy = _LC.load_policy
parse_doc_metadata = _LC.parse_doc_metadata


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize_candidates(paths: list[str]) -> list[str]:
    cleaned: set[str] = set()
    for item in paths:
        norm = item.strip().lstrip("./")
        if not norm or norm.startswith("#"):
            continue
        cleaned.add(norm)
    return sorted(cleaned)


def _find_code_hard_references(root: Path, candidates: list[str]) -> dict[str, list[str]]:
    """检查候选文件是否仍被源码、测试、独立服务或 .sandcastle 脚本硬引用。"""
    tracked = [p for p in git(root, "ls-files", "-z").split("\0") if p and (root / p).is_file()]
    code_files = [
        p for p in tracked if p.startswith(HARD_CODE_REF_PREFIXES) and not p.startswith(".sandcastle/logs/")
    ]
    needles: dict[str, list[str]] = {}
    for cand in candidates:
        tokens = [cand]
        if cand.startswith("docs/adr/"):
            tokens.append(Path(cand).name)
        needles[cand] = tokens

    hits: dict[str, list[str]] = {}
    for cf in code_files:
        text = (root / cf).read_text(encoding="utf-8", errors="ignore")
        for cand, tokens in needles.items():
            if any(tok in text for tok in tokens):
                hits.setdefault(cand, []).append(cf)
    return hits


def validate_candidates(root: Path, raw_paths: list[str], policy: dict[str, Any]) -> list[str]:
    """Fail-Closed 候选归档清单校验：拦截受保护路径、Legal Hold 与代码/测试硬依赖。"""
    candidates = _normalize_candidates(raw_paths)
    if not candidates:
        raise ValueError("归档候选清单为空")

    protected = set(policy.get("protected_paths", []))
    errors: list[str] = []
    for rel in candidates:
        if not rel.startswith("docs/"):
            errors.append(f"拒绝归档 docs/ 之外的路径: {rel}")
            continue
        if rel in protected:
            errors.append(f"拒绝归档受保护路径: {rel}")
            continue
        full = root / rel
        if not full.is_file():
            errors.append(f"候选文件不存在: {rel}")
            continue
        meta = parse_doc_metadata(full.read_text(encoding="utf-8", errors="ignore"))
        if meta["hold"]:
            errors.append(f"文件声明了 hold: true（Legal Hold），禁止归档: {rel}")

    if errors:
        raise ValueError("候选校验失败:\n- " + "\n- ".join(errors))

    code_refs = _find_code_hard_references(root, candidates)
    if code_refs:
        details = [f"{cand} <- {', '.join(refs)}" for cand, refs in sorted(code_refs.items())]
        raise ValueError("候选文件仍被代码/测试/.sandcastle 硬引用，拒绝归档:\n- " + "\n- ".join(details))

    return candidates


def _find_link_rewrite_targets(root: Path, candidate_set: set[str]) -> list[str]:
    """列出工作树中需要改写相对链接的 Markdown 文件。"""
    tracked = [p for p in git(root, "ls-files", "-z").split("\0") if p and (root / p).is_file()]
    adr_names = {Path(c).name for c in candidate_set if c.startswith("docs/adr/")}
    affected: list[str] = []
    root_resolved = root.resolve()

    for rel in tracked:
        if rel in candidate_set or not rel.endswith((".md", ".mdx")) or rel.startswith(".sandcastle/logs/"):
            continue
        if rel == "docs/adr-index.md":
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8", errors="ignore")
        needs = False
        for m in MD_LINK_RE.finditer(text):
            target = m.group(2).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            try:
                resolved = (path.parent / path_part).resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                continue
            if resolved in candidate_set:
                needs = True
                break
        if not needs and not rel.startswith("docs/validation/"):
            for m in BACKTICK_ADR_RE.finditer(text):
                if m.group(1) in adr_names:
                    needs = True
                    break
        if needs:
            affected.append(rel)
    return sorted(affected)


def plan_archive(root: Path, raw_paths: list[str]) -> dict[str, Any]:
    """只读预演：返回归档候选、体积、受影响改链文件及待冻结 ADR 编号。"""
    policy = load_policy(root)
    candidates = validate_candidates(root, raw_paths, policy)
    candidate_set = set(candidates)
    total_bytes = sum((root / rel).stat().st_size for rel in candidates)
    adr_numbers = sorted(m.group(1) for rel in candidates if (m := ADR_PATH_RE.match(rel)) is not None)
    link_files = _find_link_rewrite_targets(root, candidate_set)
    return {
        "mode": "dry-run",
        "archive_repo": policy["archive_repo"],
        "candidate_count": len(candidates),
        "total_bytes": total_bytes,
        "candidates": candidates,
        "adr_numbers_to_freeze": adr_numbers,
        "files_to_relink": link_files,
    }


def _rewrite_links_in_main(
    root: Path,
    candidate_set: set[str],
    archive_repo: str,
    archive_sha: str,
) -> list[str]:
    permalink_prefix = f"https://github.com/{archive_repo}/blob/{archive_sha}"
    adr_names = {Path(c).name for c in candidate_set if c.startswith("docs/adr/")}
    tracked = [p for p in git(root, "ls-files", "-z").split("\0") if p and (root / p).is_file()]
    root_resolved = root.resolve()
    changed_files: list[str] = []

    for rel in tracked:
        if rel in candidate_set or not rel.endswith((".md", ".mdx")) or rel.startswith(".sandcastle/logs/"):
            continue
        if rel == "docs/adr-index.md":
            continue
        path = root / rel
        orig = path.read_text(encoding="utf-8")

        def repl_link(match: re.Match[str], src_path: Path = path) -> str:
            label, raw_target = match.group(1), match.group(2).strip()
            if raw_target.startswith(("http://", "https://", "mailto:", "#")):
                return match.group(0)
            parts = raw_target.split("#", 1)
            path_part = parts[0]
            frag = f"#{parts[1]}" if len(parts) == 2 else ""
            if not path_part:
                return match.group(0)
            try:
                resolved = (src_path.parent / path_part).resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                return match.group(0)
            if resolved in candidate_set:
                return f"{label}({permalink_prefix}/{resolved}{frag})"
            return match.group(0)

        updated = MD_LINK_RE.sub(repl_link, orig)
        if not rel.startswith("docs/validation/"):

            def repl_tick(match: re.Match[str]) -> str:
                fname = match.group(1)
                if fname in adr_names:
                    return f"[ADR-{fname[:4]}（归档）]({permalink_prefix}/docs/adr/{fname})"
                return match.group(0)

            updated = BACKTICK_ADR_RE.sub(repl_tick, updated)

        if updated != orig:
            path.write_text(updated, encoding="utf-8")
            changed_files.append(rel)

    return sorted(changed_files)


def _sync_adr_index(root: Path, archived_adr_filenames: set[str]) -> bool:
    """从 docs/adr-index.md 同步移除已归档 ADR 行，并更新顶层现行计数。"""
    index_path = root / "docs" / "adr-index.md"
    if not index_path.is_file() or not archived_adr_filenames:
        return False
    orig_lines = index_path.read_text(encoding="utf-8").splitlines()
    kept_lines = [
        line for line in orig_lines if not any(f"(adr/{fname})" in line for fname in archived_adr_filenames)
    ]
    remaining_adrs = len(list((root / "docs" / "adr").glob("*.md")))
    updated = "\n".join(kept_lines) + "\n"
    updated = re.sub(
        r"当前生效的 \d+ 份核心架构决策",
        f"当前生效的 {remaining_adrs} 份核心架构决策",
        updated,
        count=1,
    )
    if updated != "\n".join(orig_lines) + "\n":
        index_path.write_text(updated, encoding="utf-8")
        return True
    return False


def apply_archive(
    root: Path,
    raw_paths: list[str],
    *,
    reason: str,
    archive_repo_url: str | None = None,
    batch_label: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """执行先存后删冷归档：推送归档仓 -> 主仓删除 -> 改写链接 -> 同步索引与策略 -> 断链自检。"""
    today = today or datetime.now(UTC).date()
    policy = load_policy(root)
    candidates = validate_candidates(root, raw_paths, policy)
    candidate_set = set(candidates)

    source_sha = git(root, "rev-parse", "HEAD").strip()
    archive_repo = policy["archive_repo"]
    remote_url = archive_repo_url or f"https://github.com/{archive_repo}.git"
    label = batch_label or f"doc-gc-{today.isoformat()}"

    with tempfile.TemporaryDirectory() as tmpdir:
        arch = Path(tmpdir) / "archive"
        subprocess.run(["git", "clone", remote_url, str(arch)], check=True, capture_output=True, text=True)

        batch_records: list[dict[str, Any]] = []
        batch_bytes = 0
        for rel in candidates:
            src = root / rel
            dst = arch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            raw = src.read_bytes()
            if dst.read_bytes() != raw:
                raise RuntimeError(f"归档复制校验失败（字节不一致）: {rel}")
            sha256 = _sha256_bytes(raw)
            title = parse_doc_metadata(raw.decode("utf-8", errors="ignore"))["title"] or Path(rel).name
            batch_bytes += len(raw)
            batch_records.append({"path": rel, "sha256": sha256, "bytes": len(raw), "title": title})

        # Rebuild MANIFEST.sha256 across all files under docs/
        all_arch_docs = sorted(
            p.relative_to(arch).as_posix() for p in (arch / "docs").rglob("*") if p.is_file()
        )
        manifest_lines: list[str] = []
        total_arch_bytes = 0
        for rel in all_arch_docs:
            raw = (arch / rel).read_bytes()
            total_arch_bytes += len(raw)
            manifest_lines.append(f"{_sha256_bytes(raw)}  {rel}")
        (arch / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        subprocess.run(["sha256sum", "-c", "MANIFEST.sha256", "--quiet"], cwd=arch, check=True)

        # Update ARCHIVE_LIST.json preserving prior batches
        list_path = arch / "ARCHIVE_LIST.json"
        if list_path.is_file():
            meta = json.loads(list_path.read_text(encoding="utf-8"))
        else:
            meta = {"source_repo": "kilbertert/AI-Ops", "batches": [], "archived_paths": []}
        batches = list(meta.get("batches", []))
        batches.append(
            {
                "label": label,
                "date": today.isoformat(),
                "source_commit": source_sha,
                "reason": reason,
                "count": len(batch_records),
                "bytes": batch_bytes,
                "records": batch_records,
            }
        )
        meta["source_commit"] = source_sha
        meta["archived_count"] = len(all_arch_docs)
        meta["archived_bytes"] = total_arch_bytes
        meta["batches"] = batches
        meta["archived_paths"] = sorted(set(meta.get("archived_paths", [])) | candidate_set)
        list_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Append batch summary to archive README.md
        readme_path = arch / "README.md"
        existing_readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else "# Archive\n"
        addition = (
            f"\n### 归档批次 `{label}`（{today.isoformat()}）\n"
            f"- **源修订**：`kilbertert/AI-Ops@{source_sha}`\n"
            f"- **原因**：{reason}\n"
            f"- **数量与体积**：{len(batch_records)} 个文件（{batch_bytes / 1024:.1f} KB）\n"
        )
        readme_path.write_text(existing_readme.rstrip() + "\n" + addition, encoding="utf-8")

        subprocess.run(["git", "-C", str(arch), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(arch),
                "commit",
                "-m",
                f"chore(archive): {label} — {len(batch_records)} files from AI-Ops@{source_sha[:8]}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        branch = git(arch, "rev-parse", "--abbrev-ref", "HEAD").strip()
        subprocess.run(
            ["git", "-C", str(arch), "push", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
        )
        archive_sha = git(arch, "rev-parse", "HEAD").strip()

    # Archive push succeeded; now mutate main working tree
    for rel in candidates:
        (root / rel).unlink()

    relinked_files = _rewrite_links_in_main(root, candidate_set, archive_repo, archive_sha)

    archived_adr_names = {Path(rel).name for rel in candidates if ADR_PATH_RE.match(rel)}
    archived_adr_nums = sorted(m.group(1) for rel in candidates if (m := ADR_PATH_RE.match(rel)))
    index_updated = _sync_adr_index(root, archived_adr_names)

    # Update docs/doc-lifecycle.json watermark & frozen numbers
    policy_path = root / "docs" / "doc-lifecycle.json"
    policy["archive_ref"] = archive_sha
    if archived_adr_nums:
        merged_nums = sorted(set(policy.get("archived_adr_numbers", [])) | set(archived_adr_nums))
        policy["archived_adr_numbers"] = merged_nums
        max_num = max(int(policy.get("adr_high_water_mark", 0)), max(int(n) for n in merged_nums))
        policy["adr_high_water_mark"] = max_num
    policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Built-in zero-broken-ADR-link verification
    visible = [
        p
        for p in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
        if p and (root / p).is_file()
    ]
    broken = _LC._find_broken_adr_links(root, visible)
    if broken:
        raise RuntimeError(f"归档后主仓仍存在 ADR 断链: {broken}")

    return {
        "mode": "apply",
        "archive_repo": archive_repo,
        "archive_sha": archive_sha,
        "batch_label": label,
        "archived_count": len(candidates),
        "archived_bytes": batch_bytes,
        "archived_paths": candidates,
        "relinked_files": relinked_files,
        "adr_index_updated": index_updated,
        "frozen_adr_numbers": archived_adr_nums,
    }


def _read_paths_args(paths_file: str | None, extra_paths: list[str]) -> list[str]:
    collected = list(extra_paths)
    if paths_file:
        collected.extend(Path(paths_file).read_text(encoding="utf-8").splitlines())
    return collected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths-file", help="每行一个相对路径的候选归档清单文件")
    parser.add_argument("--path", action="append", default=[], help="单个候选相对路径（可重复传入）")
    parser.add_argument("--reason", required=True, help="本次归档原因（写入归档仓批次元数据）")
    parser.add_argument("--archive-repo-url", help="覆盖归档仓 Git URL 或本地 bare 仓路径")
    parser.add_argument("--batch-label", help="覆盖归档批次标识（默认 doc-gc-YYYY-MM-DD）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读预览影响面（默认）")
    mode.add_argument("--apply", action="store_true", help="真实推送归档仓并更新当前主仓工作树")
    parser.add_argument("--format", choices=("json", "text"), default="text")

    args = parser.parse_args(argv)
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        raw_paths = _read_paths_args(args.paths_file, args.path)
        if args.apply:
            result = apply_archive(
                root,
                raw_paths,
                reason=args.reason,
                archive_repo_url=args.archive_repo_url,
                batch_label=args.batch_label,
            )
        else:
            result = plan_archive(root, raw_paths)

        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["mode"] == "dry-run":
            print(f"[DRY-RUN] 候选 {result['candidate_count']} 份（{result['total_bytes'] / 1024:.1f} KB）")
            print(f"  目标归档仓: {result['archive_repo']}")
            print(f"  待冻结 ADR 编号: {result['adr_numbers_to_freeze']}")
            print(f"  需自动改链文件 ({len(result['files_to_relink'])}): {result['files_to_relink']}")
        else:
            print(
                f"[APPLIED] 已归档 {result['archived_count']} 份至 "
                f"{result['archive_repo']}@{result['archive_sha'][:8]}"
            )
            print(f"  改链文件: {len(result['relinked_files'])}，索引已更新: {result['adr_index_updated']}")
        return 0
    except (subprocess.CalledProcessError, OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"文档归档失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
