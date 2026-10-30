---
title: metrics-redaction
desc: 运行指标按构造脱敏——不存问题与答案正文
hue: 30
code:
  - src/aiops_diagnostics/gateway/metrics_store.py
---
# metrics-redaction

每次完成的助手路由交互一行：租户、路由类型、智能体/版本、会话 id、检索状态、搜索与媒体计数、
延迟、token、失败码。

## raw source

**按构造脱敏**：「长期监控数据保持脱敏」这件事由**写入处的字段集合**保证，
不由读取处的约定保证。读取处脱敏意味着漏一处就漏一次；写入处不存则无处可漏。

## 不变量

- 存的一行里**没有**：问题正文、答案正文、提示词、知识库内容、媒体 URL、令牌、存储路径。
- 原始 QA 文本另存（`assistant_questions`），有自己的保留期——两处保留期是分开的决定。
- 行按 `METRICS_RETENTION_DAYS` 修剪，聚合面因此有界。
