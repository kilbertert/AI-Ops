# Codex mediated knowledge retrieval

Status: Proposed

客服 RAG 运行仍使用现有 Codex SDK harness，但 Codex 不直接访问公网、kb-service 或 RAGFlow；它通过 AI-Ops 暴露的受限 `knowledge_search` 工具自主决定是否检索。harness 负责已发布智能体版本、租户与知识库白名单、查询次数和结果规模、脱敏、引用与媒体授权，避免固定预检索削弱模型判断，同时不放宽现有只读安全边界。

## Consequences

- P0 可以保留 Codex 的工具选择和推理能力，并复用现有 `tool_requests` → 执行 → 同一 thread 继续运行机制。
- `knowledge_search` 不是通用网络工具；模型不能选择任意 `kb_id`、`top_k`，也不能生成未经检索授权的图片或视频 URL。
- 当问题意图要求知识依据而模型漏检索时，harness 必须要求补充检索；检索最多两次。
