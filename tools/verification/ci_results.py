"""读取 pytest JUnit XML；保留既有数量/跳过基线，不解析终端排版。"""

import argparse
import os
from pathlib import Path
from xml.etree import ElementTree


def check_results(path: Path, floor: int) -> str:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    passed = len(cases) - failed - skipped
    summary = f"passed={passed} skipped={skipped} failed={failed}"
    if failed or passed < floor or skipped > 10:
        raise ValueError(f"{summary}；要求 passed>={floor}、skipped<=10 且无失败")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--floor", type=int, required=True)
    parser.add_argument("--tier", choices=("full", "docs"), required=True)
    args = parser.parse_args()
    try:
        summary = check_results(args.report, args.floor)
    except (OSError, ElementTree.ParseError, ValueError) as exc:
        parser.exit(1, f"CI 测试结果不满足基线：{exc}\n")
    print(f"tier={args.tier} {summary}")
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(target).open("a", encoding="utf-8") as output:
            output.write(f"\n测试计数：`tier={args.tier} {summary}`\n")


if __name__ == "__main__":
    main()
