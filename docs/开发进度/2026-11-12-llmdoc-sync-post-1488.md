# 开发进度：llmdoc 知识层增量同步（post-#1488）

- 日期：2026-11-12
- 分支：`docs/llmdoc-sync-post-1488`（worktree `AI-Ops-llmdoc-sync-post-1488`，base `2367e25c`）
- 上一里程碑：#1489（16 篇文档全量同步 e5e952e..30bcfcbf）

## 背景

上一轮同步合并后 Stop hook 仍报 6 impacted / 1 needs-review / 101 unmapped。归因：

1. 同步审核期间 #1488（`4b9f65ec`，ADR-0250 恢复自然回复 + 币种仅观察）插队合入，其改动未被任何文档案头覆盖 → 5 篇实质 impacted。
2. 上轮未传 `--verified`，已复核未改的文档案头停在 e5e952e → `bounded-read-surfaces` 等被旧提交反复标记。
3. `execution-node-contract` 因 `requires: execution-delegation` 的上游变更进入 needs-review 待闭包。
4. unmapped 为 ADR/validation/前端 patch 等结构性噪声，非缺陷。

## 本轮内容

- 实质改写：`consumer/consumer-chat.mdx`（交付恢复自然正文+来源/媒体，删除已删模块 `consumer_order_evidence.py` 的路由与断言）、`operator/operator-chat.mdx`（币种仅观察，`unsupported_currency` 退役）。
- 复核未改：execution-model、execution-delegation、publication-lifecycle、bounded-read-surfaces、execution-node-contract（needs-review 闭包）。
- 收尾方式：`llmdoc commit --all`（全量指纹 + 推进全局 baseline），消除「永远落后窗口」的提示噪声。

## 已知缺口

- 无 doc-code 一致性自动化断言；文档正确性依赖人工/代理逐条核对代码。
- unmapped 路径（ADR、validation、web-patch 前端文件等）持续累积，需定期归类或接受为噪声地板。

## 下一步

- 后续 `/llmdoc:update` 按新 baseline 度量；若业务行为再变（如下一轮回复策略调整），对应文档即时增量复核。
