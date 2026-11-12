# llmdoc 知识层增量同步验收记录（30bcfcbf..2367e25c 窗口）

- 日期：2026-11-12
- 里程碑：`/llmdoc:update` 增量同步——仅覆盖上一轮合并后新落的代码提交与上轮遗留的记账缺口。
- 范围：delta 判定的 6 篇 impacted + 1 篇 needs-review 文档；唯一新代码提交 `4b9f65ec`（#1488，ADR-0250）。

## 方法

1. `npx -y @tokenroll/llmdoc status` / `delta --json` 定窗：6 impacted / 1 needs-review / 99 committed + 2 dirty unmapped。
2. `git rev-list HEAD ^ff3a355c -- src/` 归因：窗口内唯一新代码提交为 `4b9f65ec`；其余 impacted 为旧窗口案头未刷新（`validatedRevision` 仍 e5e952e）或 requires 闭包。
3. 逐文档核对代码事实（`consumer_agent.final_answer`、`consumer_documents.render_sources_answer`、`operator_answer_review`、`node_publication.business_prompt`），ADR-0250 为语义权威。
4.  recorder（llmdoc:recorder 子代理）在任务 worktree 内对 2 篇判为 stale 的文档改写、其余 5 篇复核后判 verified-unchanged。

## 关键事实核对

- `consumer_order_evidence.py`（程序事实呈现器）整个模块已随 #1488 删除（-290 行，含测试），`final_answer` 恢复为「Agent 自然正文 + `render_sources_answer` 程序附加来源/媒体」。
- `operator_answer_review` 不再发出 `unsupported_currency`；`currencies[]` 仅摘录观察，`station_names[]` 仍由程序对照判定 `unsupported_station`。
- `business_prompt` 现剥离 `INSTRUCTIONS`/`INSTRUCTIONS_WITH_CURRENCY_GUARD` 两种后缀以兼容已发布快照。
- 文档 `code.paths` 中对已删模块的引用同步移除（路由元数据修复）。

## 验收状态

- **未完成业务验收**：本轮是文档层同步，无业务行为变更；正确性以「文档断言与现行代码逐条对得上」为判据。
- 文档断言均经代码核对；未做 doc-code 一致性自动化断言（llmdoc 无此机制），属已知缺口。
