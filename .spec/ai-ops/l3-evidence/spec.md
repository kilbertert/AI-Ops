---
title: l3-evidence
desc: 现役有界数据与知识读取
code:
  - src/aiops_diagnostics/data/sources.py
related:
  - src/aiops_diagnostics/health/health_report.py
  - src/aiops_diagnostics/data/charging_status.py
  - src/aiops_diagnostics/knowledge/knowledge_retrieval.py
---
# l3-evidence

## raw source

公司范围、健康/分析与业务回调仍需要只读数据。旧 HTTP 设备诊断源和 Python 工具循环已删除。

## 不变量

- QueryScope 冻结授权范围，SQL 参数绑定下推；租户规则共用 order_visibility。
- 数据读取有时间窗、行数和超时上限。
- 知识、CPMS 与节点证据按各自业务任务留存；不再创建 Python 诊断工作区或 journal。
