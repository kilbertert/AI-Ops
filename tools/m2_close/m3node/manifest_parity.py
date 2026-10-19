#!/usr/bin/env python3
"""跨语言清单对照：**Gateway 与节点必须对同一份清单给出同一个结论**。

为什么需要它：两端对「合格清单」的定义一旦错开，就会出现「Gateway 收下、节点拒装」——
身份算得一致也没用，那份配置根本跑不起来。这不是设想：一份缺 `model_options` 的清单
在这条脚本存在之前确实是 Python 接受、Java 拒绝。

判据是机械的：样本目录里每个文件分别喂给两边的**真实加载器**，逐条比对
「接受 / 拒绝」，接受的还要比对**身份（原始字节 sha256）**是否逐字相同。

    uv run python tools/m2_close/m3node/manifest_parity.py             # 需要 JDK + 锁定依赖
    uv run python tools/m2_close/m3node/manifest_parity.py --python-only  # 只跑 Python 一侧

JDK 与依赖由参数/环境给出（仓库里不写私有绝对路径），缺任一即非零退出 —— 不跳过报通过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "manifest-samples"
NODE_SRC = REPO / "tools/m2_close/node/src/main/java"
# 只编清单加载器与它的异常类型：这两个类不依赖框架类，容器里没网也能编。
COMPILE_UNITS = (
    "com/trendpower/aiops/node/exec/ExecManifest.java",
    "com/trendpower/aiops/node/exec/ExecConfigViolation.java",
)


def python_verdict(path: Path) -> str:
    from aiops_diagnostics import execution_manifest

    try:
        manifest = execution_manifest.load(path)
    except execution_manifest.ExecutionManifestError:
        return "REJECTED"
    if manifest is None:
        return "DISABLED"
    return f"ACCEPTED:{manifest.identity}"


def java_verdicts(samples: list[Path], deps: Path, jdk_image: str | None) -> dict[str, str]:
    """用**真实 Java 加载器**判一遍。没有容器时退回本机 javac/java。"""
    work = Path(tempfile.mkdtemp(prefix="m3-manifest-parity-"))
    try:
        sources = [str(NODE_SRC / unit) for unit in COMPILE_UNITS] + [str(HERE / "ManifestParity.java")]
        if jdk_image:
            script = (
                "set -euo pipefail\n"
                "javac -nowarn -proc:none -cp '/deps/lib/*' -d /out "
                + " ".join("/repo/" + str(Path(s).relative_to(REPO)) for s in sources)
                + "\n"
                "java -cp '/out:/deps/lib/*' ManifestParity "
                + " ".join("/samples/" + s.name for s in samples)
                + "\n"
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--cap-drop=ALL",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    f"{REPO}:/repo:ro",
                    "-v",
                    f"{deps}:/deps:ro",
                    "-v",
                    f"{SAMPLES}:/samples:ro",
                    "-v",
                    f"{work}:/out",
                    jdk_image,
                    "bash",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        else:
            javac = shutil.which("javac")
            java = shutil.which("java")
            if not javac or not java:
                raise RuntimeError("no javac/java and no M3NODE_JDK_IMAGE")
            subprocess.run(
                [javac, "-nowarn", "-proc:none", "-cp", str(deps / "lib" / "*"), "-d", str(work), *sources],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            result = subprocess.run(
                [java, "-cp", f"{work}:{deps / 'lib'}/*", "ManifestParity", *map(str, samples)],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        if result.returncode != 0:
            raise RuntimeError(f"java side failed: {result.stdout}{result.stderr}")
        verdicts: dict[str, str] = {}
        for line in result.stdout.splitlines():
            name, _, verdict = line.strip().partition("=")
            if name:
                verdicts[name] = verdict
        return verdicts
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-only", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    samples = sorted(SAMPLES.glob("*.json"))
    if not samples:
        print(f"no manifest samples under {SAMPLES}", file=sys.stderr)
        return 2

    report: dict[str, object] = {"samples": {}, "mismatches": []}
    python_side = {path.name: python_verdict(path) for path in samples}
    report["samples"] = {
        name: {"python": verdict, "sha256": hashlib.sha256((SAMPLES / name).read_bytes()).hexdigest()}
        for name, verdict in python_side.items()
    }

    if not args.python_only:
        deps = Path(os.environ["M3NODE_DEPS"]) if os.environ.get("M3NODE_DEPS") else None
        if deps is None or not (deps / "lib").is_dir():
            print("missing M3NODE_DEPS with lib/ (locked jars)", file=sys.stderr)
            return 2
        java_side = java_verdicts(samples, deps, os.environ.get("M3NODE_JDK_IMAGE") or None)
        report["java_available"] = True
        for name, verdict in python_side.items():
            java = java_side.get(name, "MISSING")
            report["samples"][name]["java"] = java  # type: ignore[index]
            expected_accept = verdict != "REJECTED"
            java_accept = java != "REJECTED" and java != "MISSING"
            if expected_accept != java_accept:
                report["mismatches"].append(f"{name}: python={verdict} java={java}")
            elif expected_accept and java != verdict:
                # 都接受，但身份不同 ⇒ 原始字节被某一侧改写过，这比判据分歧更糟。
                report["mismatches"].append(f"{name}: identity differs python={verdict} java={java}")
    else:
        report["java_available"] = False

    report["status"] = "PASS" if not report["mismatches"] else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    else:
        print(text)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
