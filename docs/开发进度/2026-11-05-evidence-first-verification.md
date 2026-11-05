# 验证分工落地与外部复审修复

- 里程碑目标：把「实现聚焦功能、独立 Hacker 提供反例」的验证分工接入全部实现/验收入口，
  并用真实任务与外部复审证明链路可靠。
- 已完成：
  - 新增 `docs/agents/verification-workflow.md` 与 ADR 0220；AGENTS/CLAUDE/README/
    afk-workflow/CODING_STANDARDS 与 `.sandcastle/` 全部实现/验收入口改引该规则；
    ralph `review-prompt.md` 重写为只读 Hacker 并补齐派发边界；`planner.ts` 审查
    信号闸住合并（#1377）。
  - 首轮实战（#1384，ticket-check 标题规则）：两轮独立 deepseek-v4.1-flash Hacker
    共确认 3 处实现缺陷（m-flag 方案副作用、盲断言、`~~~` 围栏闸门放行），全部修复；
    300 条真实工单抽样量出 69 条被误报的告警。
  - 外部复审（结论「暂不通过」）四项全部修复：审查沙箱改为无凭据第二容器、
    `claudeProfile` 显式凭据优先于环境变量、SDK 信号改为全标记一致 + 报告非空的
    严格裁决（review 与 merger 同闸）、`!`gh issue view`` 改派发者宿主渲染；
    派发边界新增点名禁读凭据路径；`~~~` 伪闭合补齐 CommonMark 尾部空白条件。
  - 回执更正：F5「报告随容器丢弃」订正为误报（SDK 持久化于 `.sandcastle/logs/`）；
    87 分钟空转计入成本；补本进度文件。
- 未完成/遗留：
  - 受影响凭据轮换（进入 Hacker 上下文的上游 key）由宿主执行。
  - 审查隔离的代价：每个带提交 issue 多一次容器创建与 `npm ci`。
  - 节时与检错收益需更多真实任务量化；本分工不强制每轮找到漏洞。
- 证据：`docs/validation/2026-11-05-evidence-first-verification.md`（含外部复审处置表）。
