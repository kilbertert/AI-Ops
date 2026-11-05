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
  - 外部复审第一轮四项已按判据修复（PR #1397）：审查沙箱改为无凭据第二容器、
    `claudeProfile` 显式凭据优先于环境变量、SDK 信号改严格裁决（review 与
    merger 同闸）、`!`gh issue view`` 改派发者宿主渲染、禁读凭据路径入边界、
    `~~~` 伪闭合补齐尾部空白条件。
  - 外部复审第二轮（结论「仍暂不通过」）四项再修：裁决改末行独立标记、
    `.sandcastle/.env` 凭据钉空 + 容器终态环境断言、审查分支唯一化 + SHA
    核对、围栏空白收敛到 CommonMark 空格/制表符（含 NBSP 反例）。
  - 回执更正：F5「报告随容器丢弃」订正为误报（SDK 持久化于 `.sandcastle/logs/`）；
    87 分钟空转计入成本；外部复审执行者实为 Codex（非 deepseek-v4.1-flash）；
    #1397 终版复验两次派发上游停滞记为未执行；补本进度文件。
- 未完成/遗留：
  - 受影响凭据轮换由宿主执行，范围含 cliproxyapi 上游 key 与
    settings.deepseek.json 认证令牌；凭据事件在轮换前不算闭环。
  - #1403 终版已由 claude-devin / swe-2-max 独立复验；H1/H3/H4 残余由
    [后续执行边界修复](2026-11-05-reviewer-execution-boundary.md)承接，不能据 COMPLETE 忽略实测缺口。
  - 审查隔离的代价：每个带提交 issue 多一次容器创建与 `npm ci`。
  - 节时与检错收益需更多真实任务量化；本分工不强制每轮找到漏洞。
- 证据：`docs/validation/2026-11-05-evidence-first-verification.md`（含两轮外部复审处置表）。
