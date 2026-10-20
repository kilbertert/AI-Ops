#!/usr/bin/env python3
"""写 M3 执行配置清单：**两侧同一份字节**，身份是它的 sha256。

这份清单不是"再抄一遍配置"，它必须与**实际会跑到的东西**逐字相同，否则领取时按漂移拒绝。
所以每一个值都有明确来源，脚本逐条核对，任一条对不上就拒绝写：

| 清单字段 | 真实来源 |
|---|---|
| `model.provider_key` / `adapter_key` | `bootstrap_platform.py` 建平台模型配置时写的那两个值 |
| `model.model_key` | `M2LIVE_MODEL_ID`（供应商模型标识） |
| `model.endpoint` | `M2LIVE_MODEL_ENDPOINT`（节点经受控出口的那个地址） |
| `model_options` | 平台模型配置里下发给节点的选项（本栈不覆盖，说明见下） |
| `system_messages` | 平台智能体的 `instruction`（`SystemPromptHandler` 直接把它当 system 消息） |
| `tools[0]` | 节点源码里的 `ControlledChargingStatusTool` 三个常量 |

**`model_options` 的坑**：清单里的选项必须与节点**最终消费对象**上的值一致，而那个值来自
平台派发体里的 `configJson`。本栈创建平台模型配置时不传 `configJson`，所以平台给的是
它自己的默认值 —— 这份脚本把「本栈实际会下发的值」写成显式的常量并**打印出来**，需要与
平台的默认值核对。这一条在真实链路里会以 `OPTIONS` 拒绝的形式暴露，不会静默通过。

用法：

    uv run python tools/m2_close/m3live/write_exec_manifest.py --out /path/exec-manifest.json

写完打印身份（sha256）。两侧（Gateway 与节点）读的是同一个文件，所以身份天然一致；
**两份副本**各自写入时必须逐字节相同，脚本据此核对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOOL_SOURCE = (
    REPO / "tools/m2_close/node/src/main/java/com/trendpower/aiops/node/ControlledChargingStatusTool.java"
)

#: 平台建模型配置时写的两个 key（`bootstrap_platform.py`）。
PROVIDER_KEY = "openai"
ADAPTER_KEY = "openai-compatible"

#: 平台**不传 `configJson`** 时下发给节点的选项 —— 实测是**全部为空**。
#: 早先这里猜了 `{"temperature": 1.0, "max_tokens": 4096}`，第一次真实请求就被节点以
#: `OPTIONS` 拒绝（`实际 temperature=null,max_tokens=null,...`）—— 那正是守卫该做的事，
#: 也说明「猜一个值」在这里是不允许的。空字典是**准确的期望**：当前没有设置任何旋钮，
#: 将来谁设了就会构成漂移。
MODEL_OPTIONS: dict[str, object] = {}


def _java_string_literals(raw: str) -> str:
    """把若干 Java 字符串字面量（可能用 `+` 拼接）解成一个值。

    不能简单按 `+` 切分：描述文本里本身就可能有 `+`（例如 "authorized for this "
    + "conversation" 之外的写法）。这里按字面量边界扫描，遇到 `+` 之外的字符就报错 ——
    宁可失败，也不要把描述猜错一个字符（清单里它是身份的一部分）。
    """
    out: list[str] = []
    index = 0
    length = len(raw)
    expect_literal = True
    while index < length:
        char = raw[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "+":
            expect_literal = True
            index += 1
            continue
        if char != '"':
            raise SystemExit(f"非字面量片段：{raw[index : index + 20]!r}")
        if not expect_literal:
            raise SystemExit(f"两个字面量之间缺少 '+': {raw[index : index + 20]!r}")
        index += 1
        while index < length and raw[index] != '"':
            if raw[index] == "\\":
                index += 1
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
                out.append(mapping.get(raw[index], raw[index]))
                index += 1
                continue
            out.append(raw[index])
            index += 1
        index += 1
        expect_literal = False
    return "".join(out)


def _tool_constants() -> dict[str, str]:
    """从**节点源码**读受控工具的三个发布常量 —— 不在这里手抄一份。"""
    text = TOOL_SOURCE.read_text(encoding="utf-8")

    def extract(name: str) -> str:
        marker = f"public static final String {name} ="
        start = text.index(marker) + len(marker)
        # 语句结束的分号**不能在字符串里**：描述文本里就有 `;`
        # （"Takes no arguments; the order ..."）。先扫到字符串外的那个分号。
        index, in_string = start, False
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = not in_string
            elif char == ";" and not in_string:
                break
            index += 1
        return _java_string_literals(text[start:index])

    return {
        "name": extract("TOOL_NAME"),
        "description": extract("DESCRIPTION"),
        "input_schema": extract("SCHEMA"),
    }


def build(*, model_id: str, endpoint: str, system_message: str) -> dict[str, object]:
    tool = _tool_constants()
    return {
        "schema": "m3-exec-config/1",
        "name": "m2-live-03-trial",
        "history_policy": "none",
        "model": {
            "provider_key": PROVIDER_KEY,
            "adapter_key": ADAPTER_KEY,
            "model_key": model_id,
            "endpoint": endpoint,
        },
        "model_options": MODEL_OPTIONS,
        "system_messages": [system_message],
        "tools": [tool],
    }


def write(path: Path, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")
    # 清单里有期望值（含 endpoint）：与 agent-runs.json 同一条纪律，不留给同组/他人。
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model-id", default=os.environ.get("M2LIVE_MODEL_ID", ""))
    parser.add_argument("--endpoint", default=os.environ.get("M2LIVE_MODEL_ENDPOINT", ""))
    parser.add_argument(
        "--system-message",
        default=os.environ.get("M2LIVE_AGENT_INSTRUCTION", ""),
        help="必须与平台智能体的 instruction 逐字相同",
    )
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("--model-id", args.model_id),
            ("--endpoint", args.endpoint),
            ("--system-message", args.system_message),
        )
        if not value
    ]
    if missing:
        # 缺一项就不能写：清单是**期望值的唯一来源**，猜一个值等于把漂移固化成契约。
        print(f"missing required value(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    payload = build(model_id=args.model_id, endpoint=args.endpoint, system_message=args.system_message)
    out = Path(args.out)
    identity = write(out, payload)
    print(
        json.dumps(
            {"path": str(out), "identity": identity, "model_options": MODEL_OPTIONS}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
