# PROGRESS — dify-prompt-lifecycle

**开工理解**：客户端唯一业务提示词入口是「选择答复类型与知识引用」节点的 SYSTEM；管家端是编排页
提示词板块。提示词只写**表达**，权限/事实/JSON/工具/审计仍由系统负责。

**顺序**（依赖在前）：① 契约与反例 → ② 客户端事实守卫与澄清/无结果出口 → ③ 管家找站说明
（先取数再写、系统列事实）→ ④ 迁移保留运营编辑 → ⑤ 16 格与生命周期用例 → ⑥ 文档与 PR。

**风险**（≤10 行）

1. 客户端 `reply` 目前只比数字集合：金额标签互换、`每小时3元`、`已到账`、`未结束` 都可能通过。
2. 澄清/无结果/人工三类出口正文固定，改提示词对它们无效 —— 16 格里有两格因此写不出来。
3. 管家找站的正文被模板覆盖，提示词只能决定"查什么"，决定不了"怎么说"。
4. 找站说明一旦放行就是"查询前的文字"风险；判据必须是**先有工具证据**。
5. `tools/content/upgrade_consumer_flow.py --live-documents` 无条件覆盖 SYSTEM，会吞掉运营的编辑。
6. 真实模型能否稳定先调工具再写说明，只能在 41 上验；本地不得冒充。
7. 发布/恢复路径已存在（`/v1/dify/publish|retract|restore` + 页面按钮），本片只补证据，不重写。
8. 与另一会话的 `feat/dual-entry-onboarding` worktree 有文件重叠风险，本片避开
   `consumer_publication.py`、`dify_dsl_pull.py`、`self_service*.py`、`gateway_config.py`、
   `knowledge_retrieval.py`。
9. `dev-worktree start` 被该 worktree 的 dirty 状态挡住（外部原因），本片按同一约定手工建
   worktree 并在此记录，不清理他人工作。

## 进度

- [x] 读政策、ADR-0187、native-preview、current-delivery-state；重跑基线 64 passed / 0 skipped。
- [x] 任务目录与契约（acceptance.feature、qa-plan.md、PROGRESS.md）。
- [x] 客户端事实守卫（金额标签绑定、货币单位、到账承诺、状态否定）——`consumer_facts.py`。
- [x] 客户端澄清/无结果出口交提示词，系统保留受控客服入口（`validate_notice`）。
- [x] 管家找站：先取数再写说明，系统列事实，无证据即拒（`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE`）。
- [x] 迁移脚本保留运营编辑（`--replace-system` 显式开关）。
- [x] 16 格 + 两端生命周期 + 变异用例（51 项新增，六条变异逐条转红）。
- [x] 开发进度/validation 记录、入口与部署文档、操作知识自检。
- [x] PR（不合并、不部署）：[#1223](https://github.com/kilbertert/AI-Ops/pull/1223)，
      分支 `feat/dify-prompt-lifecycle`，`verify` / `Workflow policy` / `spec-tree lint` 全过，
      `mergeStateStatus=CLEAN`。**未合并、未部署。**

## 实跑结果（本机）

- 基线 64 passed（改动后同一组仍 64 passed）；全量 `tests/` 3647 passed / 9 skipped / 0 failed。
- `tools/verification/verify_prompt_lifecycle.py`：23 项，0 失败。
- 真实环境（41 的 Dify／Java 节点／模型／原 H5）**未跑**，留给 Codex 接续，见 qa-plan §五。
