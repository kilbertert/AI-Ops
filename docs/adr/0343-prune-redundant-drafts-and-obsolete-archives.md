# ADR-0343：清理已替代方案草稿、历史思考记录与过时归档，仅保留最终敲定版本

- 日期：2026-11-25
- 状态：已采纳
- 部分替代：[ADR-0340](0340-async-jobs-schema-v4-and-docs-archival.md)（仅替代其工作树保留 `docs/archive/` 的条款；ADR-0340 的 Schema v4 `async_jobs` 迁移条款继续现行）
- 范围：文档工作树与无引用旧夹具（不触碰受 `AGENTS.md` / `ADR-0097` 保护的 `docs/validation*`、`docs/开发进度*`、`docs/adr/`、`deploy/manual-deploys.md`）

## 背景

全仓逐文件审查发现，工作树中仍保留了一批已被最终方案替代或已失去运行消费者的草稿、阶段性思考日志与旧归档文件（约 1.34 MB，37 个文件）：

1. **`docs/archive/`（14 个文件）**：ADR-0340 曾将退役前旧架构图（原 `docs/architecture.md`）、已搁置 `/diag/*` 计划、M2 接线草稿与早期离线校准语料移至只读目录。由于全部内容已在 Git 历史完整留存，继续留在工作树仅增加新成员和检索工具的噪音。
2. **`docs/afk-cutover/`（5 个文件）**：历史上一次 AFK 切换的 `decisions.md` 与 `pr-{a,b,c,d}-spec.md`，无任何现役代码、测试或文档引用。
3. **`docs/architecture/` 过时快照与被替代方案（6 个文件，约 1.05 MB）**：
   - `ai-ops-architecture.{html,json,png,svg}` 描述已退役的早期确定性诊断与 Codex 内核；
   - `2026-11-13-cross-environment-tenancy.md` 与 `2026-11-14-company-gateway-onboarding.md` 均已被 `2026-11-16-manual-environment-onboarding.md` 正式替代。
   - 同时移除 `docs/architecture/repository-layout.md` 末尾已失效的 `2026-10-28 调查归档` 长段落（约 158 行）。
4. **`docs/agents/` 阶段性思考草稿与研究中间稿（10 个文件）**：
   - `dify-multitenant-design.md`（10 月 20 日 grilling 记录，顶部已有两次订正声明被 11 月方案替代）；
   - `dify-self-service-onboarding-research.md`、`operator-runtime-options-research.md`、`snail-ai-publication-research.md`（已被现行实现或 `snail-manual-onboarding-research.md` 替代的调研过程稿）；
   - `dual-entry-claude-task.md`、`dual-entry-onboarding-plan.md`（已由 `docs/acceptance/dual-entry/implementation-spec.md` 与 ADR-0189 定型承接）；
   - `env95-shared-services.md`（95 专属隧道方案已撤回，见 ADR-0342）；
   - `images-lock-parallel-conflict.md`、`fee-fields-followup/PROGRESS.md`、`consumer-order-chat/PROGRESS.md`（无引用的历史过程笔记与进度草稿）。
5. **`examples/fixtures/` 零引用旧夹具（2 个文件）**：`missing_tx_data.json`、`ykc_amount_mismatch.json` 为已退役诊断引擎夹具，无生产或测试引用（继续保留测试在用的 `ocpp_consistent.json`）。

## 决定

1. 从工作树移除上述 37 个已替代草稿、过程日志、旧归档与零引用夹具，历史追溯统一指向 Git 提交 `57c1d788`。
2. 同步更新 `README.md`、`docs/README.md`、`docs/architecture/README.md`、`docs/architecture/repository-layout.md`、`deploy/dify-36/{README.md,env.example}`、`integrations/cpms-java/README.md` 及相关手册与测试注释，确保**所有现役非冻结文档**零断链、零悬浮路径引用（受 `AGENTS.md` / `ADR-0097` 保护的 `docs/validation/`、`docs/开发进度/`、`docs/adr/` 历史回执保持只读不改写，其指向被清理旧草稿的历史链接可通过 Git 提交 `57c1d788` 追溯）。
3. 保留仍被现役手册或测试直接引用的运行事实与合同文档（例如 `docs/agents/kb-service-test-env.md`、`docs/agents/operator-cpms/sop-followup.md`、`docs/agents/env-95-facts.md`），以及受 `AGENTS.md` / `ADR-0097` 与 `test_milestone_archive_is_frozen.py` 保护的历史回执和 ADR 目录。

## 备选方案

- **方案 A（保留 `docs/archive/` 与所有历史草稿在工作树）**：不删文件，仅在顶部加过期提示。缺点是大量过时方案与 1 MB+ 旧架构图仍污染全文检索与新工程师视角，无法落实“仅保留最终敲定版本”的目标。
- **方案 B（一并删除 `docs/validation*` 与 `docs/开发进度*` 历史回执）**：会违反 `AGENTS.md`“历史失败与原始证据保留”约束并触发 `test_milestone_archive_is_frozen.py` 门禁，因此显式排除，留待用户按需单独决策。
- **方案 C（本决定：仅删可证实已被替代的草稿/过程稿/旧归档，保留受保护回执与现役引用）**：在严格遵守仓内证据治理与零断链的前提下最大化降低工作树文档噪音。

## 验证

- 运行全仓非冻结文档断链扫描，确认零新增断链与零悬浮删除路径引用；
- 运行 `tests/test_readme_architecture.py`、`tests/test_repository_layout.py`、`tests/test_milestone_archive_is_frozen.py`、`tests/tooling/test_doc_impact.py`、`tests/gateway/test_assistant_cancel_handoff.py`、`tests/gateway/test_scope_runtime.py`、`tests/integrations/test_diag_*_contract.py` 与 `ruff`。
