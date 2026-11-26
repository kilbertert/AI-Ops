# ADR-0348：瘦身 gateway_api、拆分 assistant 路由、提升延迟导入并补齐模块职责头

日期：2026-11-26
状态：已采纳
范围：`src/aiops_diagnostics/gateway/`、`src/aiops_diagnostics/identity/`、`src/aiops_diagnostics/common/config.py`、`src/aiops_diagnostics/health/rules.py`

## 背景

从全新接手工程师视角审视 `main@9bee9e1e`，Gateway 接入层存在三处显著推高首次阅读成本的历史堆积：

1. `gateway/gateway_api.py`（1,668 LOC，缺少模块职责说明）同时承担了 FastAPI 装配、71 条 `X as X` 重导出（其中 34 条全仓零外部引用）、调用者身份解析器构造链、HTTP 响应组装与统一 503 日志、以及会话轮次与订单可见性守卫；
2. `gateway/routers/assistant.py`（1,267 LOC）将 `register()` 写成 793 行单体函数，把 `/health` + 固定 FAQ、图片与签名媒体代理、核心问答派发/轮询/取消、以及 SSE 流/导出/反馈/重新生成四个互不相干的路由域混在同一闭包中；
3. 各路由文件残留上百处非循环依赖的函数体内部延迟导入（mask 了真实模块依赖），同时 10 个高频核心模块缺少顶部职责 docstring。

## 决策

1. **按职责拆出 3 个无环下游模块并清理零引用重导出（`gateway_api.py` 1,668 → ~490 LOC）**：
   - `gateway/caller_auth_wiring.py`：承接调用者身份解析器构建（`_caller_resolver` / `_company_token_resolver` / `_fallback_caller_resolver`）与统一异常映射（`_authenticate_caller` / `_platform_error`）；
   - `gateway/response_views.py`：承接作业/问答/分析/会话响应视图、`_clarification`、`_record_route_metric`、`_no_store_headers` 与 `_runtime_unavailable`；
   - `gateway/conversation_helpers.py`：承接会话查找/轮次认领释放、`_order_authorization` / `_require_visible_order`、`_operator_json` 与 `_start_operations_review`；
   - 从 `gateway_api.py` 移除 34 个全仓零外部引用的 `X as X` 重导出，保留 34 个仍被外部测试与路由引用的兼容导出及 `_chat_dispatch_hooks()` 装配入口（确保现有测试 monkeypatch 无需更改）；
   - 同步扩展 `test_caller_auth_mapping.py`、`test_gateway_order_guards.py`、`test_gateway_response_shapes.py`、`test_gateway_runtime_failures.py`、`test_order_visibility.py` 的源码静态守卫覆盖全部拆分后模块。
2. **按业务域将 `gateway/routers/assistant.py` 拆分为 4 个独立路由文件**：
   - `gateway/routers/health_faq.py`：`/health` 与 `/v1/faq/*`；
   - `gateway/routers/media.py`：`/v1/assistant/photos` 与 `/v1/media/{signed_id}`；
   - `gateway/routers/assistant_actions.py`：`/v1/assistant/questions/{qa_id}/{events,export,feedback,regenerate}`；
   - `gateway/routers/assistant.py`：核心问答创建/轮询/取消/列表及其派发辅助。
3. **提升路由层非循环延迟导入，并补齐 10 个核心模块中文职责 docstring**。

## 备选方案

- **方案 A（保持 `gateway_api.py` 与 `routers/assistant.py` 单文件，仅补注释或折叠区块）**：无法消除 1,668 行与 793 行函数造成的认知过载与跨路由域误改风险，故不采用。
- **方案 B（彻底删除 `gateway_api.py` 全部私有兼容导出并改写 30+ 测试文件的 monkeypatch 路径）**：会制造大量机械测试 diff 并增加误伤测试替身契约的风险；分阶段先抽清实现、保留已验证的兼容导出，成本最低且可零行为变化交付。

## 影响与验证

- `tests/test_readme_architecture.py` 继续强制全包（含函数内延迟导入）严格零环；
- 全部对外 HTTP 路径、鉴权策略、OpenAPI schema 与只读业务边界保持不变，并通过真实 `aiops-gateway serve` 进程探针（`verify-aiops-gateway`）验证。
