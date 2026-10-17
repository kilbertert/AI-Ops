---
title: l3-evidence
desc: 证据与诊断内核——有界只读查询、工具白名单、追加式证据日志
hue: 90
code:
  - src/aiops_diagnostics/diagnostic_tools.py
related:
  - src/aiops_diagnostics/sources.py
  - src/aiops_diagnostics/engine.py
  - src/aiops_diagnostics/journal.py
  - src/aiops_diagnostics/agent_workspace.py
  - src/aiops_diagnostics/health_report.py
  - src/aiops_diagnostics/charging_status.py
  - src/aiops_diagnostics/knowledge_retrieval.py
---
# l3-evidence

模型决定**要什么证据**，这一层决定**能给什么证据**。它是「只读边界」这句承诺的落地处。

## raw source

`DiagnosticSources` 协议有 8 个方法，多个实现（直连 MySQL/TDengine/Redis、HTTP、混合、
受范围约束、全离线 fixture）。**两个源集合并存不是历史包袱，是推论三的必然结果**：
标准 API 面携带 `QueryScope`，租户以参数绑定谓词下推；设备运行路径没有 SQL 可下推，
租户由工具层按行过滤。一个租户规则、两种渲染。**不要试图合并它们**——合并意味着要么给设备
路径伪造一个它没有的范围对象，要么放弃 SQL 下推。

## 不变量

- **工具白名单在这一层强制**，不在提示词里。白名单外的工具记 `blocked` 且**不取数**——
  拒绝要留下痕迹，否则一次越权尝试看起来像一次空结果。
- 每个证据结果存 artifact + SHA-256（`journal`）：证据可回查是"可信结论"的定义，
  不是附加功能。
- 证据日志必须落到运行目录上（`agent_workspace` 是它的磁盘契约）。
- 查询有界：存在上限，不存在"把整张表拉出来再过滤"的路径。
