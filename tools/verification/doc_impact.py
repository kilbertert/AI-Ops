"""报告相对基线的文档影响；不判断语义、不写验证台账。"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

import yaml


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def path_matches(path: str, pattern: str) -> bool:
    """目录前缀及逐段glob；**匹配零层或多层，兼容项目Python 3.11下限。"""
    pattern = pattern.split("#", 1)[0].rstrip("/")
    if path.startswith(pattern + "/"):
        return True

    def match(parts, rules):
        if not rules:
            return not parts
        if rules[0] == "**":
            return match(parts, rules[1:]) or bool(parts and match(parts[1:], rules))
        return bool(parts and fnmatch.fnmatchcase(parts[0], rules[0]) and match(parts[1:], rules[1:]))

    return match(path.split("/"), pattern.split("/"))


def report(root: Path, base: str) -> dict:
    requested = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    revision = git(root, "merge-base", requested, "HEAD").strip()
    changed = set(git(root, "diff", "--name-only", "--no-renames", "-z", revision).split("\0"))
    changed.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    changed.discard("")
    affected = []
    mapped = set()
    documents = sorted((root / "llmdoc").rglob("*.mdx"))
    if not documents:
        raise ValueError("llmdoc映射缺失，不能报告无影响")
    for doc in documents:
        lines = doc.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---" or "---" not in lines[1:]:
            raise ValueError(f"缺少frontmatter: {doc.relative_to(root)}")
        metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
        patterns = metadata["code"]["paths"]
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            raise ValueError(f"无效code.paths: {doc.relative_to(root)}")
        matches = {
            path
            for path in changed
            for pattern in patterns
            if path_matches(path, pattern)
        }
        if matches:
            affected.append({"document": str(doc.relative_to(root)), "changed_sources": sorted(matches)})
            mapped.update(matches)
    return {
        "base_sha": revision,
        "head_sha": git(root, "rev-parse", "HEAD").strip(),
        "scope": "与基线共同祖先到当前工作树（含未忽略的新文件）；复核提示，不是过期判定",
        "affected": affected,
        "unmapped": sorted(changed - mapped),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="明确的比较基线，例如 origin/main")
    args = parser.parse_args()
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        print(json.dumps(report(root, args.base), ensure_ascii=False, indent=2))
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"无法完成文档影响检查: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
