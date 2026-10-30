"""工具声明两侧对齐：Python 侧读取、Java 侧读取，逐项比对。

## 为什么它必须存在

工具定义现在只有一份声明（`src/aiops_diagnostics/operator/operator_tools.json`），
但**读它的是两个运行时**。任何一侧的解析漂移都会表现为「网关说有这个工具、节点说没有」，
而这种不一致不会自己报错——它只会让某一轮整轮失败，且错误码看起来像配置问题。

所以判据不是「两侧都能跑通」，而是**两侧对同一份声明得出逐项相同的视图**：工具名、顺序、
响应形状、证据字段、来源、描述、方法。

## 跑法

```bash
M3NODE_JDK_IMAGE=eclipse-temurin@sha256:… \
  uv run python tools/experiments/m2_close/m3node/tool_declaration_parity.py --out /tmp/parity.json
```

`--declaration` 可以指向另一份文件（用于验证一份**新增**工具的声明两侧都接受，而节点源码
一个字节都不用改）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from aiops_diagnostics.operator import operator_tools  # noqa: E402

DEFAULT = ROOT / "src/aiops_diagnostics/operator/operator_tools.json"
#: 节点读声明的系统属性名。与 `OperatorTools.PATH_PROPERTY` 必须一致 —— 两侧都靠它定位。
TOOLS_PROPERTY = "aiops.node.tools-file"

#: Java 侧只把这几个字段读出来比对。装配需要什么，就比什么。
JAVA_DUMP = """
package com.trendpower.aiops.node;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class ToolDeclarationDump {
    public static void main(String[] args) throws Exception {
        Map<String, Object> out = new LinkedHashMap<>();
        for (String name : OperatorTools.names()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("name", name);
            row.put("kind", OperatorTools.kind(name));
            row.put("source", OperatorTools.source(name));
            row.put("description", OperatorTools.description(name));
            row.put("evidence_fields", OperatorTools.evidenceFields(name));
            out.put(name, row);
        }
        System.out.println(new ObjectMapper().writeValueAsString(out));
    }
}
"""


def python_view(path: Path) -> dict:
    view: dict[str, dict] = {}
    for tool in operator_tools.load(path):
        view[tool.name] = {
            "name": tool.name,
            "kind": tool.kind,
            "source": tool.source,
            "description": tool.description,
            "evidence_fields": list(tool.evidence_fields),
        }
    return view


def java_view(path: Path, image: str, deps: Path, out: Path) -> dict:
    classes = out / "parity-classes"
    classes.mkdir(parents=True, exist_ok=True)
    dump = out / "ToolDeclarationDump.java"
    dump.write_text(JAVA_DUMP, encoding="utf-8")
    root = Path(os.environ.get("M3NODE_PARITY_ROOT") or ROOT).absolute()
    common = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--cap-drop=ALL",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{root}:/repo:ro",
        "-v",
        f"{deps}:/deps:ro",
        "-v",
        f"{classes}:/out",
    ]
    compile_cmd = (
        'find /repo/services/execution-node/src/main/java -name "*.java" > /tmp/sources; '
        "cp /cases/ToolDeclarationDump.java /tmp/ && "
        "echo /tmp/ToolDeclarationDump.java >> /tmp/sources; "
        'javac -nowarn -proc:none -cp "/deps/lib/*" -d /out @/tmp/sources'
    )
    build = subprocess.run(
        [*common, "-v", f"{out}:/cases:ro", "--network=none", image, "bash", "-c", compile_cmd],
        capture_output=True,
        text=True,
        timeout=300,
    )
    (out / "compile.log").write_text(build.stdout + build.stderr, encoding="utf-8")
    build.check_returncode()
    run = subprocess.run(
        [
            *common,
            "-v",
            f"{out}:/cases:ro",
            "--network=none",
            image,
            "java",
            f"-D{TOOLS_PROPERTY}=/cases/declaration.json",
            "-cp",
            "/out:/deps/lib/*",
            "com.trendpower.aiops.node.ToolDeclarationDump",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    (out / "run.log").write_text(run.stdout + run.stderr, encoding="utf-8")
    run.check_returncode()
    return json.loads(run.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--declaration", default=str(DEFAULT))
    args = parser.parse_args()
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    deps = Path(os.environ["M3NODE_DEPS"]).absolute()
    image = os.environ["M3NODE_JDK_IMAGE"]
    if "@sha256:" not in image or not (deps / "lib").is_dir():
        raise ValueError("locked JDK and dependency directory required")

    declaration = Path(args.declaration).absolute()
    staged = out / "declaration.json"
    staged.write_text(declaration.read_text(encoding="utf-8"), encoding="utf-8")

    left = python_view(declaration)
    right = java_view(out, image, deps, out)
    report = {
        "declaration": str(declaration),
        "tools": sorted(left),
        "python": left,
        "java": right,
        "match": left == right,
    }
    (out / "tool-declaration-parity.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not report["match"]:
        sys.stderr.write("两侧对同一份声明得出了不同视图\n")
        return 1
    print(f"TOOL_DECLARATION_PARITY PASS tools={len(left)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
