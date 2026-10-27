# 仓库整理：执行 ADR-0097 的归档冻结

日期：2026-10-27。分支 `docs/repo-archive-cleanup`，基线 `4066ab42`。PR #1104。

## 目标

用户判断仓库层次混乱、文件冗余，怀疑"旧版本 AI-Ops 那套已被取代的代码"可以整体删除。
本轮先做梳理，再只整理"规则定了却没被执行"的部分。

## 梳理结论（决定了本轮范围）

- **不存在"已取代的旧 AI-Ops 代码"**。1141 个跟踪文件里没有一份是旧架构残留：
  `java/`（3 个）是待落地工件且仍被 `docs/diag-query-api-plan.md` 引用；
  `tools/m2_close/`（57 个）是 M2/M3 验收工具，`qa-plan.md` 与 `RELEASE-NOTES.md` 仍在用。
- **12G 目录里只有约 410M 是仓库内容**，其余是 gitignored 的构建产物
  （`.scratch/dify-1.17.1` 8.3G、`.scratch/company-repos` 1.6G、
  `deploy/dify-36/web-patches/work` 207M）。按用户指示本轮不动物理空间。
- 真正的问题：**ADR-0097 已把两个追加式文档冻结为只读归档，但没有任何东西会失败**，
  于是 #1014、#1015 又直接往归档里追加了整节。

## 已完成内容

- 把 #1015 追加的两节迁出归档：`docs/validation/2026-10-23-real-cross-user-denial.md`、
  `docs/开发进度/2026-10-23-real-cross-user-denial.md`。**正文逐字未改**，逐行 diff 核对过。
- 两份归档加冻结声明头（指向 ADR-0097）。
- 新增 `tests/test_milestone_archive_is_frozen.py` + 基线
  `tests/milestone_archive_headings.json`（496 个标题）：归档只许少、不许多。
- 补上 `docs/agents/issue-tracker.md` 里缺的、指向 `ticket-contract.md` 的那一行
  （AFK 模板本来就有，我们的副本漏了）。

## 判据的一处返修（Devin 审查发现）

第一版判据是「不许出现晚于冻结日的标题」。它有洞：**#1015 的标题就是冻结当天（2026-10-23）
的日期**，`>` 放它过去；改成 `>=` 又会把归档里本来就有的那批 10-23 旧记录全部判红。
改为**基线集合比对**：出现基线里没有的标题即红，与日期无关。变异验证——
追加 `## 2026-10-23 · 冻结当天追加的新验收` ⇒ **1 failed / 4 passed**，还原后全绿。

## 未完成 / 已知缺口

- **README 的入链补丁未提交**（已 stash 在本工作树）：`AI-Ops-consumer-knowledge-picker`
  工作树有未提交的 README 改动，路径级 push 守卫会拦。**六篇孤立文档的入链因此仍缺**——
  它们本身没有删除、内容完整，只是 `README.md` 索引表里还没加那五行。下一轮补。
- 物理空间未整理（用户明确本轮不做）。
- `docs/validation.md`（21952 行）与 `docs/开发进度.md` 的历史内容一字未拆，按 ADR-0097
  作为归档保留。
