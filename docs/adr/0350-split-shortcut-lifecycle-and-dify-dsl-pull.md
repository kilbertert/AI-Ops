# ADR-0350：按无环三层结构拆分 shortcut_lifecycle 与 dify_dsl_pull

日期：2026-11-26
状态：已采纳
范围：`src/aiops_diagnostics/publication/`、`src/aiops_diagnostics/dify/`、`tests/`

## 背景

在完成 Gateway 接入层瘦身（ADR-0349）后，仓库仍存在两个超过 1,300 行的领域巨石模块：

1. `publication/shortcut_lifecycle.py`（1,495 LOC）把领域模型与常量、230+ 行多语言内置种子数据（`_BUNDLED_SHORTCUTS`）、翻译覆盖检查、SQLite CRUD 存储（`ShortcutStore`）、字段校验器以及生命周期管理器（`ShortcutManager`）全部塞在同一个文件内；
2. `dify/dify_dsl_pull.py`（1,327 LOC）把 Dify 导出/控制台只读 HTTP 客户端（`DifyDslClient`、`DifyDslError*`）、YAML DSL 纯映射规则（`map_dsl_to_config`、`workflow_content` 等）以及智能体草稿收敛 / Workflow 快照同步编排逻辑混在同一文件中。

新接手工程师定位单个问题时，需要在 1,300~1,500 行上下文中反复跳转识别纯函数、HTTP 客户端、SQLite 查询和业务规则边界。

## 决策

按职责将其各自拆分为严格单向无环的三层模块栈，同时保持对外符号契约与现有测试替身（monkeypatch）边界零破坏：

1. **`publication/shortcut_*` 三层拆分**：
   - `publication/shortcut_models.py`（底层模型）：承接 `Shortcut`、`ShortcutVersion`、`ShortcutCopyGap`、异常类（`ShortcutError*`）、权限与路由常量、`_BUNDLED_SHORTCUTS`、`shortcut_copy_gaps`、`_warn_missing_translation` 与纯字段校验器；保留原有 logger 名称以兼容日志断言；
   - `publication/shortcut_store.py`（中层存储）：承接 `ShortcutStore`（SQLite CRUD 与租户生效视图归并）与行映射器 `_shortcut_from_row` / `_version_from_row`；通过 `gateway_db.connection` 保持锁纪律，不反向依赖 `shortcut_lifecycle`；
   - `publication/shortcut_lifecycle.py`（顶层编排）：承接 `ShortcutManager` 并通过 `X as X` 重导出既有公共与测试兼容符号。
2. **`dify/dify_dsl_*` 三层拆分**：
   - `dify/dify_dsl_client.py`（底层客户端）：承接 `DifyDslSource`、`DifyDslClient`、`DifyDslError*`、路径常量与响应信封解包/错误映射辅助；
   - `dify/dify_dsl_mapping.py`（中层映射）：承接 `map_dsl_to_config`、版本与数据集/工具声明校验及 `workflow_content` 等纯映射函数；
   - `dify/dify_dsl_pull.py`（顶层编排）：保留 `SyncOutcome`、`resolve_pulled_config`、`pull_agent_draft`、`converge_agent_draft`、`sync_workflow_from_dify`、`commit_workflow_snapshot` 与 `sync_binding_from_dify`，在本地模块命名空间直接实例化 `DifyDslClient` 并重导出底层与中层符号，保证所有测试对 `dify_dsl_pull.DifyDslClient` / `sync_binding_from_dify` 的 monkeypatch 行为完全不变。
3. **同步迁移静态守卫清单**：
   - 将 `tests/common/test_scalar_rules.py` 的 `_DELEGATED_STORE_FILES` 登记从 `publication/shortcut_lifecycle.py` 迁移至 `publication/shortcut_store.py`；
   - 将 `tests/identity/test_order_identity.py` 的 `_ALLOWED_ALPHABET_LITERALS` 登记从 `dify/dify_dsl_pull.py` 和 `publication/shortcut_lifecycle.py` 精确迁移至实际持有对应正则的 `dify/dify_dsl_client.py` 与 `publication/shortcut_models.py`。

## 备选方案

- **方案 A（仅拆分两个文件但取消顶层重导出，批量改写全仓导入与测试 monkeypatch 路径）**：会连带修改 20+ 测试和调用点，提高与并行分支冲突及误伤测试语义的风险；在顶层模块保留轻量 `X as X` 重导出可先安全落地结构解耦，后续按需渐进清理。
- **方案 B（仅在原文件内补充注释分段，不做文件拆分）**：无法缩减单文件千行体积，也无法用模块边界隔离纯解析、SQLite 存储与 HTTP 客户端。

## 影响与验证

- 两个子包均保持严格无环（`tests/test_readme_architecture.py`），文件归属符合 `tests/test_repository_layout.py`；
- HTTP 路由、存储表结构、日志通道、错误语义与既有测试行为保持不变，通过全量 `ruff`、`pytest`、`compileall`、`uv build` 与 `adr.check.mjs` 验证。
