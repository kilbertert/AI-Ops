---
title: l4-inference
desc: 业务路线与一次性模型请求
code:
  - src/aiops_diagnostics/model_request.py
related:
  - src/aiops_diagnostics/consumer/consumer_intent.py
  - src/aiops_diagnostics/consumer/consumer_chat_service.py
  - src/aiops_diagnostics/ops_analysis_narrative.py
  - src/aiops_diagnostics/dify_trial.py
  - src/aiops_diagnostics/answer_language.py
  - src/aiops_diagnostics/publication/qa_rag.py
  - src/aiops_diagnostics/publication/agent_debug.py
  - src/aiops_diagnostics/dify_dsl_pull.py
  - src/aiops_diagnostics/faq.py
  - src/aiops_diagnostics/publication/agent_manifest.py
  - src/aiops_diagnostics/clarification.py
  - src/aiops_diagnostics/dify_tools_catalog.py
  - src/aiops_diagnostics/dify_console_publish.py
  - src/aiops_diagnostics/dify_knowledge_api.py
  - src/aiops_diagnostics/publication/agent_content.py
  - src/aiops_diagnostics/dify_debug_identity.py
  - src/aiops_diagnostics/publication/shortcut_migration.py
---
# l4-inference

## raw source

C端走已登记 Dify 工作流，管家走 Java 节点。试用按应用绑定走对应草稿路线。
Python Thin Harness、诊断 CLI、通用问答/分类回退不属于当前架构。

## 不变量

- 入口缺少业务绑定时明确拒绝，不回落旧执行器或旧 FAQ 匹配。
- C端语义分类和聚合说明只有一次有界 HTTP 请求，不生成工具调用、任意 shell 或持久线程。
- 结构化输出不完整、拒绝、工具调用均拒绝；语言守卫在业务定稿点执行。
- 发布/知识元数据校验保留，旧 Python 草稿与工具名单不再执行。
