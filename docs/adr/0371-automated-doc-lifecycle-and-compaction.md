---
status: active
---

# 371. 建立「写入控制 → 失鲜检测 → 触发回收 → 一键迁冷」文档自动减债闭环（Doc-GC）

日期：2026-11-28
状态：已采纳；整合既有 `llmdoc`、`docs/adr-index.md`、`AI-Ops-history-archive` 与链接门禁，建立长期防腐机制，避免再次累积数百份历史债务。

## 背景

[ADR-0345](0345-offload-legacy-receipts-to-archive-repo.md) 与 [ADR-0370](0370-archive-superseded-and-tactical-adrs.md) 两次集中清理已将 973 份历史回执、进度日志及 231 份历史/局部 ADR 迁出至私有归档仓库，但本质上仍属于“事后人工大扫除”（对应数据库 `VACUUM FULL`）。如果写入端继续只追加、缺少机读墓碑与日落条款、缺少周期性可达性巡检与一键归档工具，后续持续迭代中文档债务仍会重新膨胀。

## 决定

采用 100% 确定性规则（零模型门禁、不修改 `afk-bootstrap` 管理的 `.sandcastle/adr.check.mjs`），在项目自管工具层建立四段闭环：

1. **策略配置与高水位线锁（`docs/doc-lifecycle.json`）**：
   - 集中声明归档仓地址/修订、231 个已归档 ADR 永久冻结编号清单（防止新 ADR 复用旧号）、Autovacuum 阈值（墓碑冷静期 30 天、不可达回执冷静期 60 天、`llmdoc` 强漂移 20 次提交、膨胀线：墓碑 ADR ≥ 12 / 现行 ADR ≥ 160 / 不可达回执 ≥ 30）、GC Roots 与受保护路径。
2. **写入控制与失鲜检测引擎（`tools/verification/doc_lifecycle.py`）**：
   - 兼容可选 YAML frontmatter（`status` / `supersedes` / `superseded_by` / `expires_after` / `last_reviewed` / `review_cycle_days` / `hold`）与既有中英文状态行，不强迫回改存量 125 份 ADR；
   - `check-pr` 子命令实施 5 条确定性写入门禁（禁复用已归档编号、声明 `supersedes` 须同 PR 对称标旧 ADR 为 `superseded`、`temporary` 须写 `expires_after`、触碰文档零 ADR 断链、新增现行 ADR 须入 `docs/adr-index.md`），并以非阻断提醒输出“删除/改名路径幽灵引用”与 `llmdoc` 映射影响；
   - `audit` 子命令每周自动巡检墓碑冷静期、日落到期、复核超期、`llmdoc/meta.json` 累积漂移、从 GC Roots 出发的不可达旧验收回执及膨胀水位。
3. **一键冷归档引擎（`tools/verification/doc_archive.py`，Phase 2）**：
   - 将先存后删、SHA-256 校验、`MANIFEST.sha256`/`ARCHIVE_LIST.json` 更新、固定 `<ARCHIVE_SHA>` permalink 改写、`docs/adr-index.md` 与 `docs/doc-lifecycle.json` 同步封装为带 `--dry-run` / `--apply` 与 Legal Hold 保护的标准 CLI。
4. **GitHub Actions 闭环自动化（`.github/workflows/doc-lifecycle.yml`，Phase 3）**：
   - PR 触发 `check-pr` 写入 Step Summary 并拦截硬错误；每周日定时触发 `audit` 幂等维护唯一一张滚动维护 Issue（清零时自动关闭），绝不无人值守直接删除主分支文档。
   - 定时与手动巡检共用仓库级并发组，排队更新同一张台账，不中断现有巡检；PR 门禁仍按各自 PR 串行并取消过期检查。
   - 复用 `fleet-kit` 后，并发控制仍由 AI-Ops 调用任务负责；共享工作流实现台账逻辑，不代管本仓队列。

## 备选方案

- **方案 A（定时任务全自动直接删除文档并 push `main`）**：被否。文档语义与合规留存存在边界判断，无人值守静默删文风险不可控；正确做法是机器完成 100% 确定性检测、分诊、打包校验与生成 PR，人工只做最终 Merge 拍板。
- **方案 B（直接魔改 `.sandcastle/adr.check.mjs` 或引入大模型门禁）**：被否。`.sandcastle/adr.check.mjs` 随 `afk-bootstrap` 同步升级，本地分叉会导致后续同步冲突；大模型门禁不可复现且易抖动，不符合本仓确定性门禁原则。
- **方案 C（一次性给全部 125 份存量 ADR 补写 YAML frontmatter）**：被否。无业务增益且制造大量噪音 diff；解析器同时兼容可选 YAML frontmatter 与既有中英文状态行即可平滑演进。

## 后果

- **收益**：文档治理从“攒满后人工大扫除”演进为“写入即防腐 + PR 耦合告警 + 每周 Autovacuum 巡检 + 一键 SHA-256 归档”，显著压低后续认知读放大与人工清理成本。
- **代价**：新增现行 ADR 时需同步在 `docs/adr-index.md` 增补 1 行并遵守 `0372+` 水位线；若新 ADR 取代旧 ADR，须在同 PR 更新旧 ADR 状态为 `superseded`。

## 验证

- **Phase 1**（PR #1820 / `f5e27ab0`）：`tests/tooling/test_doc_lifecycle.py`（覆盖全部 5 条硬门禁、幽灵引用、squash 合并后的 `llmdoc` 漂移回退、GC 不可达回执、Legal Hold 与现仓全量校验）与真实仓库 `doc_lifecycle.py audit` 验证通过。
- **Phase 2**（PR #1821 / `6501c9b2`）：`tests/tooling/test_doc_archive.py`（本地双 bare 仓离线端到端归档、SHA-256 校验、permalink 改写、索引同步、编号冻结与 Fail-Closed 拦截）及真实仓库 `--dry-run` 演练通过。
- **Phase 3**（`.github/workflows/doc-lifecycle.yml`）：`tests/tooling/test_doc_lifecycle_workflow.py`（工作流触发器、SHA 锁定、最小权限、无表达式注入及假 `gh` 驱动滚动台账 Issue 建/改/关/空跑 4 分支测试）与 PR 实测 `Doc Lifecycle / PR doc check` 通过。
