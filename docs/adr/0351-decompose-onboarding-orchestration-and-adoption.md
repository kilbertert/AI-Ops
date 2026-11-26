# ADR-0351：拆解 onboarding 编排与执行环境迁移恢复并补充模块辨析索引

日期：2026-11-26
状态：已采纳
范围：`src/aiops_diagnostics/onboarding/`、`docs/agents/environment-pipeline-map.md`

## 背景

在完成核心域巨石拆分（ADR-0349、ADR-0350）后，`onboarding/` 子包仍存在三类影响新工程师首次阅读的复杂度来源：

1. `onboarding/orchestration.py` 中的 `_advance_one()` 单函数长达 324 行，把取消汇聚、子阶段扫描、`site-preflight` 新鲜度门禁、`candidate-install` 预派发续窗、`site-preflight-refresh` 派发、下一阶段子任务派生及异常阻断映射混在同一个深层锁作用域中；
2. `onboarding/execution_adoption.py`（1,322 LOC）将只读预演/正向迁移计划与备份清单校验/CAS 恢复实现放在同一文件；
3. `onboarding/management.py`（30+ 处）与 `onboarding/publication.py`（25 处）在函数体内部大量使用非循环依赖的延迟导入，且 `onboarding/` 周围存在多组命名高度相似的 `*trial*`、`*knowledge*`、`publication/release/validation/business/readiness` 模块，新工程师容易混淆职责入口。

## 决策

1. **按阶段拆解 `onboarding/orchestration.py` 中的 `_advance_one()`**：
   - 将取消态记录、阶段进度扫描、`site-preflight` 门禁检查、`candidate-install` 窗口续签、`site-preflight-refresh` 派生、下一阶段子任务派生及异常阻断映射提取为职责单一的同文件私有辅助函数；
   - `_advance_one()` 主体保持 ~60 行自顶向下状态分发逻辑，全部既有阶段顺序、状态迁移语义与测试替身绑定完全不变。
2. **拆出单向无环恢复模块 `onboarding/execution_adoption_restore.py`**：
   - 承接 `SCHEMA`、`AdoptionError`、共用校验/CAS 辅助（`_assert_schema_present`、`_assert_no_inflight_work`、`_verify_active_bindings_precommit`、`_public_summary` 等）及备份校验与 CAS 恢复（`_validate_backup_manifest`、`_restore_from_backup`）；
   - `onboarding/execution_adoption.py` 保留正向预演/迁移主流程 `adopt()`，仅单向依赖 `execution_adoption_restore`（零环），并在顶部重导出 `SCHEMA`、`AdoptionError` 与恢复相关兼容符号。
3. **提升 `onboarding/management.py` 与 `onboarding/publication.py` 中的非循环延迟导入**：
   - 统一提升至模块顶部导入，对路由与阶段协同子模块优先采用 `from aiops_diagnostics.onboarding import <module>` 模块导入，确保测试对子模块属性的 monkeypatch 继续生效，同时维持 `test_readme_architecture.py` 全包严格无环。
4. **更新 `docs/agents/environment-pipeline-map.md`**：
   - 登记新增模块 `execution_adoption_restore.py`；
   - 增加 `## 易混淆模块辨析速查` 对照表，明确区分三类 `*trial*.py`、三类 `*knowledge*.py` 与 `publication/release/validation/business/readiness` 的边界。

## 备选方案

- **方案 A（将 `execution_adoption.py` 保留为 1,322 行单文件、仅重排函数顺序）**：仍超出千行且把恢复态的逐表漂移校验混在正向迁移路径内，不利于独立审阅恢复契约。
- **方案 B（通过新建子包目录重新归类全部 `onboarding` 与 `trial/knowledge` 文件）**：大规模改路径违背 `test_repository_layout.py` 与 ADR-0346 的克制原则，极易与并行交付分支冲突；先以模块内拆解 + 流水线辨析速查表降低接手认知成本，收益最高且零路径破坏。

## 影响与验证

- `tests/test_readme_architecture.py` 继续验证全包（含函数内导入）零循环依赖，`tests/test_repository_layout.py` 目录规则通过；
- 全量 `ruff`、`pytest`、`compileall`、`uv build` 与 `adr.check.mjs` 验证通过。
