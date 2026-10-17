---
title: l4-inference
desc: 推理与产品能力——模型 harness、容错解析、以及各回答面
hue: 60
code:
  - src/aiops_diagnostics/agent_engine.py
related:
  - src/aiops_diagnostics/codex_runtime.py
  - src/aiops_diagnostics/agent_runner.py
  - src/aiops_diagnostics/agent_validator.py
  - src/aiops_diagnostics/turn_recovery.py
  - src/aiops_diagnostics/routing.py
  - src/aiops_diagnostics/qa_rag.py
  - src/aiops_diagnostics/faq.py
  - src/aiops_diagnostics/promo_agents.py
  - src/aiops_diagnostics/answer_language.py
  - src/aiops_diagnostics/agent_manifest.py
  - src/aiops_diagnostics/agent_debug.py
  - src/aiops_diagnostics/clarification.py
  - src/aiops_diagnostics/dify_dsl_pull.py
  - src/aiops_diagnostics/dify_trial.py
  - src/aiops_diagnostics/dify_tools_catalog.py
  - src/aiops_diagnostics/dify_console_publish.py
  - src/aiops_diagnostics/dify_knowledge_api.py
  - src/aiops_diagnostics/agent_content.py
  - src/aiops_diagnostics/dify_debug_identity.py
  - src/aiops_diagnostics/shortcut_migration.py
  - src/aiops_diagnostics/__main__.py
---
# l4-inference

模型在哪里跑、它被允许做什么、它的输出怎么被接住。

## raw source

`agent_engine.AgentCoordinator` 是整个 agent 路径的心脏：turn 循环、跨 provider 容错解析、
工具分发、合同修复循环、完成/阻断终态。**模型不可信这条推论在这里兑现**——
不是靠提示词约束模型，而是靠 harness 在它周围架设边界。

## 不变量

- **答案面的语言守卫只有一处判定**（`i18n`），各回答面在**定稿点**调用同一份应用
  （`answer_language`）。**不得用提示词兜底**——那是把一条可检查的规则换成一句建议。
- 传输层的容错解析是**显式的修复**，不是静默容忍：围栏 / 最外层 `{}` / 首部丢失修复，
  每一处都对应一个实测过的上游行为。
- 澄清答复用稳定的非本地化 `CLARIFY_*` 码：同一条码在**响应体、指标行、结构日志**三处
  是同一个串。此前六个分支共用一句本地化文案，排查只能靠把每条分支跑一遍量字节数。
- 跑在**零阶工作区**上的模型轮看不到订单数据；那是"只能解释、不能去查证"的护栏。
- 工具集合只可**收窄**，不可扩张；请求体里的租户/站点/用户字段一律**拒绝而非忽略**。
