# 知识层增量同步验收（post-#1491，ADR-0251）

- 日期：2026-11-12
- 范围：`f99565f5..402e7e4d`（运行日志行级明细 v2、schema 版本闸修正、部署回执）
- 基线推进：12 篇受影响文档指纹已刷新（`9c5184e` + meta `c22094f`）

## 逐篇处置

| 文档 | 处置 | 依据 |
|---|---|---|
| gateway-store | 改写 `_version()` 段落 | 0..VERSION 接受已知历史库、check() 旧版报告、v1→v2 在 41 实测自动迁移（`PRAGMA user_version`=2、`tool_summaries_json` 在场） |
| execution-delegation | 扩展 `operator_run_logs.py` 投影段 | `record_tool_summary` 有界写、`_tool_summaries` 防御读、last-call-wins |
| operator-chat | 增补 ADR-0251 摘要/明细段 | `tools=N`、`allowed`、`_summarize_tool` 两路、吞错面 |
| dify-self-service | 管理面行级明细一句 | qa_id+scope_fingerprint 门禁不变 |
| delivery-and-hosts | `code.paths` 登记本轮两个 web-patch 文件 | 机制原文已覆盖，不改 prose |
| conversation-store、dify-console-access、dify-execution-publishing、execution-model、gateway-runtime、publication-lifecycle、test-architecture | `--verified` 核验未变 | 仅经 `gateway_schema.py` 或测试断言字面量受影响，prose 仍然为真 |

## 执行的检查

- `npx @tokenroll/llmdoc validate`：ok
- `npx @tokenroll/llmdoc commit --verified …`：12 篇指纹刷新（docs `9c5184e` + meta `c22094f`）
- Devin 复审后修订：`tools=0` 不落 detail 的措辞更正（`operator-chat.mdx`）

## 未覆盖项

- `execution-node-contract.mdx` 的 needs-review 标记未处理（与本轮运行日志改动无关，留待后续专项核验）。
- 其他 `deploy/dify-36/web-patches/1.17.1/` 文件未登记进 `code.paths`（目录级归属决策留待后续）。
- `docs/agents/current-delivery-state.md` 保持有意不映射（易变索引，不绑稳定文档评审义务）。
