#!/usr/bin/env python3
"""复跑本地提示词契约与版本生命周期；页面、真实模型另记验收。"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = [
    "tests/consumer/test_prompt_binding_lifecycle.py",
    "tests/consumer/test_prompt_fact_segments.py",
    "tests/consumer/test_consumer_prompt_migration.py",
    "tests/operations/test_prompt_publication_lifecycle.py",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    out = (args.out or Path(tempfile.mkdtemp(prefix="prompt-lifecycle-"))).resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("输出目录必须为空，不能覆盖旧证据")
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pytest", "-q", *TESTS, "--junitxml=" + str(out / "tests.xml")]
    result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out / "tests.log").write_text(result.stdout)
    print(result.stdout)
    suites = ET.parse(out / "tests.xml").getroot() if (out / "tests.xml").exists() else []
    summary = {
        key: sum(int(s.get(key, "0")) for s in suites) for key in ("tests", "failures", "errors", "skipped")
    }
    report = {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "repository": str(ROOT),
        "command": command,
        "exit_code": result.returncode,
        "checks": summary,
        "environment": "临时数据库、真实Gateway API、合成外部依赖",
        "not_run": ["Dify编辑器页面", "真实模型风格", "生产部署及原H5验收"],
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
