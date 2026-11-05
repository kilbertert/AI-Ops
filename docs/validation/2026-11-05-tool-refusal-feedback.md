# 工具拒绝回喂与原始码归因验证记录（2026-11-05）

范围：网关 `operator_cpms`/`gateway_api`/`operator_answer_review` 与执行节点
`CpmsReadTool`/`GatewayClient`/`ReportingChatSessionRuntime` 的配套改造。
基线 239f8bec，worktree `AI-Ops-tool-refusal-model-feedback`。

## 自动化证据

- `tests/operations/test_operator_cpms.py`、
  `tests/operations/test_operator_failure.py`、
  `tests/operations/test_operator_answer_review.py`：**101 passed**。
  其中新增 `test_business_refusals_are_marked_for_model_feedback_and_recorded`
  以真实 FastAPI 路由驱动整轮：未提及订单 → 403 `OPERATOR_ORDER_NOT_REFERENCED`
  + `feedback:true` + `hint`；缺契约头 → 403 `OPERATOR_TOOL_CONTRACT_OUTDATED`
  **无** feedback 字段；billing 代取（object 契约 `request-target/2`）→ 403
  `OPERATOR_DETAIL_REQUIRED` + `feedback:true`；随后正常取证并完成，快照
  `tool_refusals` 恰为两条按序登记，复核实参同表。
- `test_node_failure_reaches_caller_without_readback` 参数化新增
  `tool_refused:OPERATOR_SCOPE_CHANGED`、`tool_refused:cpms_unavailable` 两行：
  真实路由接受该 reason 形状，run.reason 逐字保留原始码，用户侧仍为
  `OPERATOR_DEPENDENCY_UNAVAILABLE`。
- 补跑完成回调契约相邻套件
  `test_app_runtime_node_entry.py`/`test_m3_consumer_snapshot.py`/
  `test_m3_user_entry_tool.py`/`test_m3_node_link_completion.py`/
  `test_repository_layout.py`：**146 passed**。

## 起服务实测

`verify-aiops-gateway`（throwaway 数据根，未使用真实 `AIOPS_DATA_HOME`）：
`/health` 200 只读、匿名问答/管理 401、公开 OpenAPI 200、
旧 `/v1/runs` `/v1/enroll` `/v1/standard/diagnoses` `/v1/dify/tools/*` 均 404，
all checks passed。证据：`dist/verification/gateway/aiops-gateway.json`
（worktree 内）。

## Java 侧

本机无 JDK/mvn。`docker run eclipse-temurin:21-jdk`（只读挂载 worktree 与
`~/.cache/m3node-deps` 锁定依赖、无网络、无特权）`javac -nowarn -proc:none`
全量编译通过。未做节点运行时实测。

## 未覆盖（如实声明）

- F1–F6 部署后验收（docs/acceptance/tool-refusal-feedback-qa-plan.md）未执行：
  需要新网关与新节点同时在位，合并后按 CD 顺序落地再补。
- 回喂结果在真实模型上的表现（是否据 hint 自行修正/如实说明）属模型行为，
  单测只证明链路与数据形状；以 F1–F3 实测为准。
