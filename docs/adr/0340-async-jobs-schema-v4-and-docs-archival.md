# ADR-0340: 将 `standard_diagnoses` 原子迁移为 `async_jobs`（Schema v4）并精简新人文档入口

Status: Accepted
Date: 2026-11-25

## 背景

完成代码库模块化收敛（P0–P1 与 P2.7 配置分组）后，仍存在两处遗留的认知摩擦：

1. **数据库幽灵命名**：Gateway SQLite 表 `standard_diagnoses`（索引 `idx_standard_diagnoses_*`）最初来自已退役的旧标准诊断引擎，如今实际承载运维分析四卡作业与 Dify/节点受管试运行记录，表名与真实职责严重脱节，新工程师阅读 `gateway_schema.py` 与 `gateway_store.py` 必须依赖口述历史才能明白它与已退役诊断无关。
2. **文档入口噪音**：`README.md` 随时间在首尾累计堆叠了十余段运维公告、局部故障提示与临时迁移说明（近 200 行），冲淡了 30 分钟上手主线；`docs/` 根层同时混放了已退役旧架构图（`docs/architecture.md`）、早期方案探索（`diag-query-api-plan.md`、`handoff-brief.md`、`m2-close-01-wiring.md`、`thin-harness.md` 等）与离线阈值校准 JSON，导致新人检索文档时极易误读过期材料。

## 决策

1. **Gateway Schema 升级至 `VERSION = 4`，将 `standard_diagnoses` 原子重命名为 `async_jobs`**：
   - 在 `src/aiops_diagnostics/gateway/gateway_schema.py` 中新增显式声明 `LEGACY_TABLE_RENAMES = {"standard_diagnoses": "async_jobs"}` 与 `LEGACY_INDEX_RENAMES`（`idx_standard_diagnoses_scope_created` → `idx_async_jobs_scope_created`、`idx_standard_diagnoses_scope_kind_created` → `idx_async_jobs_scope_kind_created`）。
   - 只读检查 `check()` 对 `user_version < 4` 的旧库透明识别旧表/旧索引并验证其列与约束；若旧表与新表同时存在则立即以 `SchemaError("ambiguous legacy table rename: ...")` fail-closed 拒绝，绝不猜测真值。
   - `ensure()` 在同一写事务内先校验旧库合法性，再执行 `ALTER TABLE "standard_diagnoses" RENAME TO "async_jobs"`、`DROP INDEX IF EXISTS` 旧索引、补齐任何历史缺失列、创建新索引、以 `legacy=False` 复验完整 v4 形态并写入 `PRAGMA user_version=4`；任一步骤异常均整事务回滚，确保既有数据零丢失。
   - 同步更新 `gateway/gateway_store.py`、`common/async_job_lifecycle.py`、`.spec/ai-ops/l5-persistence/job-lifecycle/spec.md` 与全部测试，并新增 v3 含数据迁移、失败回滚原子性与双表冲突拒绝的专项回归测试。
2. **重建 30 分钟新人 `README.md` 入口并建立 `docs/archive/` 只读归档目录**：
   - 精简 `README.md` 为「30 分钟必读路线 + 架构图与进程拓扑表 + 安全与数据边界 + 全包职责入口 + 本地验证命令 + 权威文档导航」，将零散运行公告收回各自权威手册与 `docs/agents/current-delivery-state.md`。
   - 将 13 份已退役/历史探索/离线校准的跟踪根层文档（含 `docs/architecture.md`、`diag-query-api-plan.md`、`handoff-brief.md`、`jev-*.{md,json}`、`m2-close-01-wiring.md`、`synthetic-acceptance-data.md`、`thin-harness.md`、`客户端调整.md`）通过 `git mv` 归入 `docs/archive/` 并补充 `docs/archive/README.md` 与 `docs/README.md` 导航。
   - 严格保持 ADR-0097 冻结归档（`docs/validation.md`、`docs/开发进度.md`）及现役按日回执目录（`docs/validation/`、`docs/开发进度/`）原位不动。

## 备选方案（被否）

- **新建 `async_jobs` 表同时保留 `standard_diagnoses` 视图或双写**：被否。`gateway_schema.py` 明确拒绝受管表上的 view/trigger，双写会引入两套真值与同步漂移风险；SQLite 原生 `ALTER TABLE ... RENAME TO` 在单事务内即可零拷贝原子完成且由 `rehearse()`/`integrity()` 护航。
- **直接删除历史探索与校准文档而非归档**：被否。历史设计上下文与校准语料在审计与复盘时仍有参考价值，移入只读 `docs/archive/` 既清除根层噪音又完整保留历史可追溯性。

## 后果

- **正面**：Gateway 数据库表名与实际语义一致（`async_jobs` 承载异步分析/试运行作业）；新工程师打开仓库即可在 30 分钟内通过精简 `README.md` 建立完整系统心智模型，`docs/` 根层不再混杂退役架构图与校准 JSON。
- **代价与运维注意**：已有生产/测试环境的 Gateway SQLite（v3）在升级至本修订时，首次运行 `aiops-admin schema migrate` 或 Gateway 启动 `startup()` 会自动将 `standard_diagnoses` 原子重命名为 `async_jobs` 并升级至 `user_version = 4`；按照 `gateway_schema._version()` 的 fail-closed 契约，升级到 v4 后不可直接回退到仅认识 v3 的旧版二进制读取同一数据库文件（如需演练或备份，升级前须使用 `aiops-admin schema rehearse`）。
