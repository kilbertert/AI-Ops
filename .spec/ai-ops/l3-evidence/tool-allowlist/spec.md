---
title: tool-allowlist
desc: 模型可点名的工具由一个白名单强制，白名单外不取数
hue: 90
code:
  - src/aiops_diagnostics/diagnostic_tools.py#DiagnosticToolExecutor
---
# tool-allowlist

模型决定要什么证据；这一层决定给不给。白名单在这里强制（ADR-0019）。

## raw source

**白名单必须在执行器里强制，不能在提示词里描述。** 提示词是建议，执行器是约束；
一个"模型应该不会点名不存在工具"的推断不构成边界。

## 不变量

- 白名单外的工具记 `blocked` 且**不取数**——拒绝要留痕，否则一次越权尝试与一次空命中
  在日志里长得一样。
- `ALL_TOOL_NAMES` 由枚举推出（默认值自动带上新工具），因此加工具的改动面是
  `ToolName` + 描述 + 执行分支，不是到处补清单。
- 环境预检是 **advisory**：它报告，不否决。

- 合成执行节点的 `GovernedToolRuntime` 覆盖默认解析器，只返回一个受控零参数工具。
  shell、通用 HTTP、RAG 与技能入口的逐个拒绝由 C 隔离实验验证；动态 MCP 连接未实测。
