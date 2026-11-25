# 334. 降低全仓接手认知成本：清零导入环、拆解网关路由与领域执行器

状态：已采纳（阶段一落地：保持全部公开 HTTP 契约、权限边界、部署入口与跨组件兼容不变；跨主机独立进程拆包与 30 文件环境接入目录重组留待独立阶段 PR）。

## 背景

全仓零背景接手审计显示，系统核心只读安全底线扎实，但胶水层长期叠加出四类偶发复杂度：

1. **导入图冻结环**：`execution.agent_runs` 与 `dify.dify_chat_workflow` 仅因 `canonical_gateway_url` 反向依赖 `gateway.gateway_config`，叠加 `chat_execution` 的类型标注，导致 `test_readme_architecture.py` 冻结了 4 模块 6 条循环导入边；
2. **网关上帝模块与巨型函数**：`gateway_api.py` 膨胀至 7,652 行（`create_gateway_app()` 单函数 4,392 行 / 108 嵌套闭包，`assistant_questions()` 433 行且 docstring 过时），`gateway_config.validate()` 297 行（含不可达死代码），`self_service_bridge.handle()` 485 行 / 116 分支；
3. **运行时与管家执行器职责堆积**：`GatewayRuntime`（2,409 行）混装运维分析四卡聚合、健康报告作业与 206 行嵌套闭包媒体代理；`OperatorCpms`（2,603 行）混装无状态证据契约与站点分页；`consumer_chat_service.py` 交错 `callback_version` 1–5 兼容分支与 v6 主路径；`environment_task_worker.py` 内联 9 段无缩进字符串拼接 SSH 脚本；
4. **隐性契约与同名双义**：`dify-console-bridge` 由 `build-bundle.sh` 手工打包子集文件但缺少导入自包含守卫；`PLATFORM_TENANT_ID` 在 Dify 工作区（`"platform"`）与快捷动作（`"__platform__"`）两处同名不同值。

## 决定

在既有目录规范与公开契约零变化的前提下，一次性落地以下六个内聚结构收敛：

1. **清零包内绝对导入循环**：将 `canonical_gateway_url` 下沉至 `common/endpoint_policy.py`（`gateway_config.py` 保留兼容导入），移除 `chat_execution.py` 对 `gateway_config` 的 AST 依赖，清空 `test_readme_architecture.py` 的 6 条冻结循环边，强制包内绝对导入图（含延迟导入）0 循环（注：`_import_edges()` 沿用既有口径未解析相对导入，`gateway/environment_*` 与 `dify/environment_*` 间既有 34 条相对导入环留待后续 `onboarding/` 收敛阶段治理）；同步将 `GatewayServerSettings.validate()` 拆分为 5 个职责校验方法并清理死代码。
2. **网关 HTTP 路由按域拆解**：将 `gateway_api.py` 拆为 `gateway/errors.py`、`gateway/schemas.py`、`gateway/dependencies.py` 与 `gateway/routers/` 下 9 个领域模块，每模块暴露 `register(app, context, deps, api)` 直接向 `FastAPI` 注册扁平 `APIRoute`（兼容既有 OpenAPI/鉴权映射扫描与测试替身入口），同时将 `assistant_questions` 拆为 4 个线性策略函数并更新文档。
3. **领域执行体归位**：从 `GatewayRuntime` 拆出 `operator/ops_analysis_runner.py`、`health/health_job_runner.py`、`knowledge/media_service.py`、`gateway/chat_job_runner.py` 与 `gateway/trial_job_runner.py`（保留薄委托、显式 `Protocol` 注入契约与 monkeypatch 钩子，移除 `gateway_api.py` 的 `suppress(Exception)` 吞异常注入，并同步补齐 `trial_job_runner` 终态写检查与聊天/试用执行体 AST 守卫）；从 `OperatorCpms` 拆出 `operator/cpms_evidence.py`、`operator/cpms_sites.py`、`operator/cpms_task_store.py` 与 `operator/cpms_proxy.py`；将 `consumer_chat_service.py` 中 `callback_version < 6` 历史兼容路径收口至 `consumer/legacy_callbacks.py`。
4. **发布桥声明式重构与打包自包含守卫**：将 `self_service_bridge.handle()` 重构为声明式动作路由表与独立校验器，并在 `tests/test_console_bridge_deployment.py` 新增 AST 守卫 `test_console_bridge_bundle_imports_are_self_contained`，拦截发布桥子集包新增未打包的包内导入。
5. **远端脚本模板收口**：将 `environment_task_worker.py` 内联的 9 段 SSH Python 脚本抽离至 `gateway/environment_remote_scripts.py` 的 `textwrap.dedent` 模板函数，保持远端退出码与参数注入语义不变。
6. **命名消歧与兼容过渡**：引入语义明确的 `DIFY_PLATFORM_WORKSPACE_ID = "platform"`（`dify_workspaces.py`）与 `SHORTCUT_PLATFORM_SCOPE = "__platform__"`（`shortcut_lifecycle.py`），同时保留 `PLATFORM_TENANT_ID` 过渡别名以避免与并行分支及未同步运维手册冲突。

## 备选方案

- **使用 FastAPI `app.include_router(APIRouter)` 替代 `register(app, ...)`**：实测 FastAPI 0.13x 下 `include_router` 向 `app.routes` 写入惰性 `_IncludedRouter` 包装器而非扁平 `APIRoute`，会直接破坏 `public_openapi.py` 与鉴权覆盖测试，因此选用与 `self_service_api.py` / `environment_management.py` 一致的扁平 `register()` 形态。
- **在同一 PR 内把 `dify-console-bridge` / `dify-login-proxy` 迁入 `services/` 并把 30 个 `environment_*` 文件迁入新一级包 `onboarding/`**：该操作跨越 systemd unit、多机手工部署链及二十余个活跃环境接入分支，违反阶段合入与「每 PR 单逻辑增量」原则，且会引发大面积分支冲突，故留作独立后续阶段 PR。
- **在本次 PR 直接删去 `PLATFORM_TENANT_ID` 旧名并改写并行分支占用文件**：经核查当前存在未合并并行 worktree 修改 `dify_console_publish.py`、`dify_login_service.py`、`admin_cli.py` 与 `docs/agents/env-41-runbook.md`，直接修改会触发跨 worktree 重叠阻断且破坏混合版本兼容，故本阶段先立新权威常量并留过渡别名。

## 影响与恢复

- 公开 `/v1/*` 路由、鉴权、只读边界、数据库表结构与 `aiops-admin` 入口均未变化；Gateway CD 仅发布 Python Gateway 进程，不要求同步重启远端 Java 节点、Dify 控制台桥或登录代理。
- 若发现回归，可直接按标准回滚流程恢复上一版 Gateway 制品，或通过 PR revert 本次提交，无数据/schema 迁移残留。
