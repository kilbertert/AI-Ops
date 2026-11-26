# ADR-0352：归并 TurnCompletion / DifyRun 参数团并提升 gateway_runtime 延迟导入

日期：2026-11-26
状态：已采纳
范围：`src/aiops_diagnostics/conversation/`、`src/aiops_diagnostics/dify/`、`src/aiops_diagnostics/gateway/`、`src/aiops_diagnostics/consumer/`、`src/aiops_diagnostics/operator/`

## 背景

在前序结构拆解完成后（ADR-0349/0350/0351），问答运行时主链路上仍保留三处影响新工程师阅读与修改安全性的参数与导入噪音：

1. `ConversationStore.complete_turn()`（18 个参数）与 `GatewayRuntime._complete_conversation_turn()`（18 个参数）沿历史演化累积了十余个完成元数据参数，由 `operator/operator_cpms.py`、`gateway/chat_job_runner.py`、`consumer/consumer_chat_service.py` 逐个透传；
2. `DifyRunStore.issue()`（20 个参数）要求每个调用方显式铺开 13~19 个具名参数构造执行记录；
3. `gateway/gateway_runtime.py` 在 `__init__`、`from_settings` 与多处业务方法体内保留了 20+ 处非循环依赖的函数体延迟导入，掩盖了 `GatewayRuntime` 的真实模块依赖面。

## 决策

1. **收敛轮次完成元数据为 `TurnCompletionMetadata`**：
   - 在 `conversation/conversation_store.py` 新增冻结数据类 `@dataclass(frozen=True, slots=True) class TurnCompletionMetadata`；
   - `ConversationStore.complete_turn()` 与 `GatewayRuntime._complete_conversation_turn()` 新增可选 `metadata: TurnCompletionMetadata | None = None` 参数并完整保留既有关键字参数签名的向后兼容（确保外部测试与同步态门禁检查不受影响）；
   - 将 `gateway_runtime.py`、`operator_cpms.py`、`chat_job_runner.py`、`consumer_chat_service.py`、`conversation_store.release_turn()` 内部转发迁移为构造并传递 `TurnCompletionMetadata`。
2. **收敛 Dify 运行签发参数为 `DifyRunSpec`**：
   - 在 `dify/dify_run_store.py` 新增冻结数据类 `@dataclass(frozen=True, slots=True) class DifyRunSpec` 与 `DifyRunStore.issue_spec(spec: DifyRunSpec)` 主实现，原 `issue(...)` 构造 `DifyRunSpec` 并委托 `issue_spec(...)`；
   - 将 `consumer_chat_service.py`、`consumer_trial.py`、`dify_workflow_trial.py`、`chat_job_runner.py` 四处内部调用点迁移至 `issue_spec(DifyRunSpec(...))`。
3. **提升 `gateway/gateway_runtime.py` 非必需延迟导入**：
   - 保持 `package_version()` 中隔离循环依赖的 `from aiops_diagnostics import __version__` 及测试 monkeypatch 勾子（如 `scoped_live_sources`、`offline_source`、`data_sources.scoped_mysql_source`、`consumer_chat_service`）动态解析能力不变；将其余 22 处非循环函数体延迟导入提升至模块顶部。

## 备选方案

- **方案 A（直接删除 `complete_turn()` 与 `DifyRunStore.issue()` 原有扁平关键字参数）**：要求同步重写大量既有测试调用，并与 `tests/conversation/test_conversation_turn_shapes.py` 的 AST 同步状态写入守卫冲突；保留原签名作为向后兼容薄委托可实现零测试改写平滑收敛。
- **方案 B（通过 `**kwargs` 透传简化签名）**：会丢失类型约束与字段自动补全，无法帮助新接手工程师识别字段契约，故选用显式冻结 `@dataclass`。

## 影响与验证

- 全包保持严格零循环依赖（`tests/test_readme_architecture.py`）；
- 对外 HTTP 路由、鉴权逻辑、OpenAPI schema、SQLite 表结构及只读业务边界零变化；
- 通过全量 `ruff`、`pytest`、`compileall`、`uv build`、`adr.check.mjs` 与临时数据根上的真实 `aiops-gateway serve` 进程探针（`verify-aiops-gateway`）验证。
- 探针与测试边界声明：本地 `verify-aiops-gateway` 仅证明 Gateway API 契约、鉴权模型与退役路由状态码，**不能**证明远端模型驱动诊断运行、KB 活性或 Dify 真实链路；此外依据 ADR-0348，合入 `main` 仅完成集成验证，生产部署到 41 主机仍须后续显式触发版本发布批次。
