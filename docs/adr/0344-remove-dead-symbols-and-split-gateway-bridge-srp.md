# ADR-0344：清理 12 处零调用死代码并按单一职责拆分 `gateway_api` 与 `dify_console_bridge`

日期：2026-11-25
状态：已采纳
范围：`src/aiops_diagnostics/`、`services/dify-console-bridge/`、`ops/dify-console-bridge/build-bundle.sh`、`tests/test_console_bridge_deployment.py`

## 背景

全仓代码结构、命名与单一职责审查发现两类影响可维护性的问题：

1. **零调用死代码残留（12 处）**：全仓 AST 交叉引用核对显示，以下 12 个顶层符号在生产源码与全部测试中均为零调用：
   - `common/private_files.py`: `append_private_text`
   - `consumer/knowledge_access.py`: `KnowledgeGrantError`
   - `consumer/legacy_callbacks.py`: `is_pre_picker_binding`
   - `dify/dify_knowledge_migration.py`: `MigrationReport`
   - `dify/dual_entry.py`: `entry_for_scenario`
   - `execution/chat_execution.py`: `route_row`
   - `knowledge/ragflow_console.py`: `describe_failure`
   - `onboarding/publication.py`: `recover_source_reference`
   - `operator/ops_analysis_narrative.py`: `ops_narrative_schema`
   - `admin/common.py`: `_deployment_environment_or_exit`
   - `consumer/consumer_chat_service.py`: `_live_answer_differs`
   - `consumer/consumer_facts.py`: `_state_term_hit`
2. **两处上帝模块违反单一职责原则（SRP）**：
   - `services/dify-console-bridge/dify_console_bridge/bridge.py`（2,206 行）：单文件混杂配置常量解析、有界出站 HTTP 客户端、发布/试运行/原生管家预览业务编排与 `BaseHTTPRequestHandler` HTTP 处理层，与旁路姊妹服务 `services/dify-login-proxy/`（已按 `core`/`company`/`provision`/`service`/`server` 清晰拆层）结构不对称。
   - `src/aiops_diagnostics/gateway/gateway_api.py`（3,003 行）：虽已将 FastAPI 路由拆入 `routers/`，主装配模块内仍滞留两段合计约 1,500 行的非装配业务编排——Dify 应用发布/恢复/草稿试运行编排，以及 C 端订单问答分发、运行回读守卫与授权快照读取。

## 决定

1. **删除上述 12 处已核实的零调用死代码**及随之失效的冗余导入，保持所有公开 CLI/HTTP 入口行为不变。
2. **按职责拆层 `services/dify-console-bridge/dify_console_bridge/`**：
   - `config.py`：配置模型 `BridgeSettings`、路由/限额常量、错误与结果数据类、来源/UUID/角色解析；
   - `client.py`：有界出站 HTTP（`_request`）、Cookie 解析、工作区/身份/知识同步与入口上游客户端；
   - `workflows.py`：发布（`publish_current_app`）、试运行提交/读回（`submit_trial`/`read_trial`）与原生管家预览 SSE 流编排；
   - `bridge.py`：精简为薄 HTTP 处理器（`ConsolePublishBridgeHandler`、`make_server`、`main`）与向后兼容导出层，通过显式 `request_fn` / `native_gateway_fn` 注入保持零循环依赖与全部既有测试替身兼容；
   - 同步更新 `ops/dify-console-bridge/build-bundle.sh` 与 `tests/test_console_bridge_deployment.py` 的打包清单与自包含 AST 守卫。
3. **按职责拆分 `src/aiops_diagnostics/gateway/gateway_api.py`**：
   - `gateway/dify_publish_ops.py`：承接 Dify 绑定应用发布、历史版本恢复、受管试用草稿构建与发布审计账本；
   - `gateway/consumer_chat_dispatch.py`：承接 C 端订单问答分发、节点/Dify 运行结果回读守卫与授权充电/账务快照组装，通过 `ChatDispatchHooks` 显式回调注入接驳认证、会话槽位与可测试数据源；
   - `gateway/gateway_api.py` 收缩为应用装配、认证解析、通用响应/指标辅助与薄转发入口，维持全包严格零循环依赖（`gateway_api` → `consumer_chat_dispatch` → `dify_publish_ops` 单向无环）。

## 备选方案

- **方案 A（保持 `bridge.py` 与 `gateway_api.py` 单文件现状，仅删死代码）**：无法解决 2,200–3,000 行大文件职责混杂问题，新工程师阅读 Gateway 装配或控制台桥仍需跨数千行上下文跳转。
- **方案 B（同步对 `dify/`、`consumer/`、`gateway/` 40 余个模块做前缀统一重命名并合并四处 `_amount()`）**：重命名会引发大面积导入、测试与 `llmdoc/` 扰动；四处 `_amount()` 分别承担严格 `Decimal` 报错、宽容 `Decimal` 返回 `None`、原样字符串裁剪与 `float` 数值化，语义并不等价，强行合并会引入错误抽象。因此刻意不执行，并在审查总结中明确说明取舍。
- **方案 C（本决定：零环单向拆层 + 显式回调注入 + 精准删除 12 处零调用死代码）**：不破坏任何外部 API、打包边界或测试替身语义，同时将两处最大模块清晰收敛为单一职责分层。

## 验证

- `ruff check .` 与 `ruff format --check .` 全绿；
- 全量 `pytest`（含零循环导入门禁 `test_readme_architecture.py`、布局门禁 `test_repository_layout.py`、发布桥自包含打包门禁 `test_console_bridge_deployment.py`、订单标识与标量规则门禁）；
- `python -m compileall`、`uv build`（wheel/sdist 构建）；
- 使用 `verify-aiops-gateway` skill 在一次性临时数据根实启 Gateway 验证外部 API 契约与鉴权模型（边界说明：本地验证不替代远端 Dify/Java 联调、真实模型调用与生产业务验收）。
