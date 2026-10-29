#!/usr/bin/env python3
"""跨语言清单对照：**Gateway 与节点必须对同一份清单给出同一个结论**。

为什么需要它：两端对「合格清单」的定义一旦错开，就会出现「Gateway 收下、节点拒装」——
身份算得一致也没用，那份配置根本跑不起来。这不是设想：一份缺 `model_options` 的清单
在这条脚本存在之前确实是 Python 接受、Java 拒绝。

**判据不拿任何一侧的结果当正确答案**：`manifest_samples_expected.json` 为每份样本声明
预期结论，脚本分别检查两侧是否满足，再检查两侧是否一致。只检查「两侧一致」会有一个
假通过：把所有样本以 0644 复制过去，两端连正常样例都拒绝，接受数量为 0，脚本照样 PASS。
所以这里还要求**至少一个正例真的被接受**。

语义样本先复制到一次性目录并**显式设置 0600**：Git 不保存 0600，检出通常是 0644，
而权限本身就是判据之一 —— 直接读工作树里的文件会让「0644 全部拒绝」变成常态。
权限负例（0644 / 符号链接）另行构造，不依赖检出权限。

    uv run python tools/m2_close/m3node/manifest_parity.py             # 需要 JDK + 锁定依赖

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
EXPECTED_FILE = HERE / "manifest_samples_expected.json"
NODE_SRC = REPO / "services/execution-node/src/main/java"
# 只编清单加载器与它的异常类型：这两个类不依赖框架类，容器里没网也能编。
COMPILE_UNITS = (
    "com/trendpower/aiops/node/exec/ExecManifest.java",
    "com/trendpower/aiops/node/exec/ExecConfigViolation.java",
)

#: 语义样本的合规权限。Git 不保存 0600，所以不能假定检出后的模式。
SEMANTIC_MODE = 0o600


def python_verdict(path: Path) -> str:
    from aiops_diagnostics.execution import execution_manifest

    try:
        manifest = execution_manifest.load(path)
    except execution_manifest.ExecutionManifestError:
        return "REJECTED"
    if manifest is None:
        return "DISABLED"
    return f"ACCEPTED:{manifest.identity}"


def stage_samples(staged: Path) -> None:
    """把语义样本复制到一次性目录并显式设权限；再构造两个权限负例。"""
    for source in sorted(SAMPLES.glob("*.json")):
        target = staged / source.name
        shutil.copyfile(source, target)
        target.chmod(SEMANTIC_MODE)
    # 权限负例：内容完全合规，只是可被组/他人读到 ⇒ 两端都必须拒绝。
    permissive = staged / "reject-mode-0644.json"
    shutil.copyfile(SAMPLES / "accept-normal.json", permissive)
    permissive.chmod(0o644)
    # 符号链接负例：指向合规文件，但路径本身是链接 ⇒ 两端都必须拒绝。
    link = staged / "reject-symlink.json"
    link.unlink(missing_ok=True)
    link.symlink_to(staged / "accept-normal.json")


def java_verdicts(samples: list[Path], staged: Path, deps: Path, jdk_image: str | None) -> dict[str, str]:
    """用**真实 Java 加载器**判一遍。没有容器时退回本机 javac/java。"""
    work = Path(tempfile.mkdtemp(prefix="m3-manifest-parity-"))
    try:
        sources = [str(NODE_SRC / unit) for unit in COMPILE_UNITS] + [str(HERE / "ManifestParity.java")]
        names = " ".join("/samples/" + sample.name for sample in samples)
        if jdk_image:
            script = (
                "set -euo pipefail\n"
                "javac -nowarn -proc:none -cp '/deps/lib/*' -d /out "
                + " ".join("/repo/" + str(Path(s).relative_to(REPO)) for s in sources)
                + "\n"
                "java -cp '/out:/deps/lib/*' ManifestParity " + names + "\n"
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
                    f"{staged}:/samples:ro",
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
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    if not EXPECTED_FILE.is_file():
        print(f"missing independent expectations: {EXPECTED_FILE}", file=sys.stderr)
        return 2
    raw_expected = json.loads(EXPECTED_FILE.read_text())
    expected = {name: spec for name, spec in raw_expected.items() if not name.startswith("_")}
    if not any(spec["accept"] for spec in expected.values()):
        # 没有正例的对照表本身没有意义：它会在一端全坏时也「一致」。
        print("expectations declare no positive sample", file=sys.stderr)
        return 2

    staged = Path(tempfile.mkdtemp(prefix="m3-manifest-samples-"))
    report: dict[str, object] = {"samples": {}, "failures": [], "positives_accepted": 0}
    try:
        stage_samples(staged)
        # 语义样本 + 两个权限负例一起喂，负例的期望也写在对照表里。
        expected["reject-mode-0644.json"] = {"accept": False}
        expected["reject-symlink.json"] = {"accept": False}
        samples = [staged / name for name in sorted(expected)]
        for sample in samples:
            if not sample.exists() and not sample.is_symlink():
                print(f"declared sample is missing: {sample.name}", file=sys.stderr)
                return 2

        python_side = {sample.name: python_verdict(sample) for sample in samples}
        report["samples"] = {
            name: {
                "expected_accept": expected[name]["accept"],
                "python": verdict,
                "sha256": (
                    hashlib.sha256((staged / name).read_bytes()).hexdigest()
                    if (staged / name).is_file()
                    else None
                ),
            }
            for name, verdict in python_side.items()
        }

        deps = Path(os.environ["M3NODE_DEPS"]) if os.environ.get("M3NODE_DEPS") else None
        if deps is None or not (deps / "lib").is_dir():
            print("missing M3NODE_DEPS with lib/ (locked jars)", file=sys.stderr)
            return 2
        java_side = java_verdicts(samples, staged, deps, os.environ.get("M3NODE_JDK_IMAGE") or None)
        report["java_available"] = True

        failures: list[str] = report["failures"]  # type: ignore[assignment]
        positives = 0
        for name, spec in expected.items():
            want = bool(spec["accept"])
            row = report["samples"][name]  # type: ignore[index]
            python_verdict_text = row["python"]
            python_accept = python_verdict_text.startswith("ACCEPTED")
            # 1) 每一侧都必须满足**独立声明**的期望，而不是「与另一侧相同」。
            if python_accept != want:
                failures.append(f"{name}: python={python_verdict_text} expected_accept={want}")
            if java_side:
                java = java_side.get(name, "MISSING")
                row["java"] = java
                java_accept = java.startswith("ACCEPTED")
                if java_accept != want:
                    failures.append(f"{name}: java={java} expected_accept={want}")
                # 2) 都接受时身份必须逐字相同（原始字节没有被某一侧改写）。
                if want and python_accept and java_accept and java != python_verdict_text:
                    failures.append(f"{name}: identity differs python={python_verdict_text} java={java}")
            if want and python_accept and java_side and java_side.get(name, "").startswith("ACCEPTED"):
                positives += 1
        report["positives_accepted"] = positives
        if positives == 0:
            # 两端全部拒绝会让「两侧一致」成立 —— 那不是对照通过，是两边都没在干活。
            failures.append("no positive sample was accepted by both sides (vacuous agreement)")
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    report["status"] = "PASS" if not report["failures"] else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    else:
        print(text)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
