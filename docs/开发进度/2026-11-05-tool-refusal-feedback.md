# 工具业务拒绝回喂模型与原始拒绝码归因

- 里程碑目标：兑现 2026-11-05 优链充路由恢复复盘中立项的待改项——「拒绝按
  业务结局回喂模型/映射业务错误码 + 节点保留原始拒绝码」。
- 已完成：
  - 网关对**模型能自己处理**的业务拒绝（查无此单/参数非法/依赖未满足/越权
    等 12 个码，`FEEDBACK_REFUSALS`）在 `/v1/internal/` 响应体上加
    `feedback:true` 与中文 `hint`；用户侧路径与授权/发布/并发完整性拒绝
    不带该标记。
  - 任务级拒绝登记 `task.refusals`（去重、封顶 16）：grant/proxy/record 三
    条路径的 `OperatorRefused` 统一经由 `_note_refusal` 过滤记录；进入完成
    快照 `tool_refusals` 与答复复核输入。
  - 答复复核 SYSTEM 增加一条规则：有对应拒绝时「查无此单/无权查看/缺少条
    件」不算幻觉，没有对应拒绝时不得声称被拒绝；`REVIEW_VERSION` 随
    SYSTEM 散列自动换版。
  - 节点（Java）：`GatewayRefused` 解析 `feedback`/`hint`；`CpmsReadTool`
    对带标记的拒绝返回普通工具结果 `{"refused":true,"code","message"}`，
    不终结整轮；`get()` 非 200 同样按标记分流。不可回喂的拒绝仍以
    `tool_refused:<原始码>` 上报终局。
  - 终局归因：`NodeLoopCompletionRequest.reason` 放宽为
    `^([a-z0-9_]*|tool_refused:[A-Za-z0-9_]{1,80})$`；网关 `_complete` 认
    该前缀并把原始码记入 `run.reason`（`operator_node_failed: tool_refused:<码>`），
    对用户仍映射 `OPERATOR_DEPENDENCY_UNAVAILABLE`。
  - 单向兼容：旧节点忽略 `feedback`/`hint` 字段，行为与现状一致；新节点遇
    旧网关时无标记可走原失败路径。两侧可独立灰度。
- 当前分支/提交：worktree `AI-Ops-tool-refusal-model-feedback`，基线 239f8bec。
- 验证结果：docs/validation/2026-11-05-tool-refusal-feedback.md；
  验收用例 docs/acceptance/tool-refusal-feedback.feature 与
  tool-refusal-feedback-qa-plan.md（F1–F6，部署后按租户实测）。
- 已知缺口：F1–F6 为部署后验收，需新网关+新节点到齐；Java 侧仅静态编译
  校验（无本地 JDK/mvn，已用 eclipse-temurin:21-jdk + 锁定依赖编译通过）。
- 下一步：合并后按 CD 先部署节点再跑网关（节点制品闸门）；到齐后执行
  F1–F6 验收。
