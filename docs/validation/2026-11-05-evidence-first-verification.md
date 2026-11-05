# 2026-11-05：验证分工落地（实现聚焦功能，独立 Hack 提供反例）

**变更**：新增 `docs/agents/verification-workflow.md` 与 ADR 0220；AGENTS.md、CLAUDE.md、
README.md、docs/afk-workflow.md 与 `.sandcastle/` 全部实现/验收入口改引该分工；
`.sandcastle/review-prompt.md` 重写为只读 Hacker；`.sandcastle/planner.ts` 让审查信号
真正闸住合并（此前结果被丢弃）。

**性质**：文档/流程改动 + 一处调用方修复。非用户可见行为变更；按政策低风险内部改动，
`acceptance.feature`/`qa-plan.md` 不适用——按本分工要求改为指令入口与角色行为演练验收。

## 确定性检查（实现者，worktree AI-Ops-evidence-first-verification）

| 检查 | 结果 |
|---|---|
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 1546 files already formatted |
| `git diff --check` | 通过 |
| `node .sandcastle/policy-check.mjs workflows` | 通过 |
| `pytest tests/test_repository_layout.py test_readme_architecture.py test_ci_test_tier.py` | 41 passed |
| `dev-worktree audit` | passed: worktrees=23 |

ADR 编号 0220 = origin/main 最大号 0219 + 1（检查时点 2026-11-05）。

## 调用方核对（实现者）

- `planner.ts`（ralph）：原代码丢弃 `sandbox.run(review-prompt)` 返回值，COMPLETE/BLOCKED
  均不影响合并；旧提示词下审阅者直接改码提交故损失较小，只读化后反例会无人消费。
  已修：审查未回报 COMPLETE 的分支按失败处理、不进当轮合并。
- `review/review.ts`（Actions agent:review）：axis 两份 `<output>` JSON 被解析并注入
  `prompt.md`（修复者），输出契约未变，链路成立。
- `main.ts`（npm run afk 单 issue）：只跑 `implement.md`，不消费 review-prompt，无影响。
- `Dockerfile` AFK 沙箱 `claude-deepseek` shim 确实剥离 `--model`（`shift 2`），
  与验证分工第 72-73 行声明一致；交互式 `claude-deepseek`（`~/.local/bin`）透传参数，
  `--model deepseek-v4.1-flash` 有效；cliproxyapi `config.yaml` 存在该别名（629、678 行）。

## 独立 Hack 演练

**执行**：`claude-deepseek -p --model deepseek-v4.1-flash`（独立会话，只读工具）。
**身份核验**：返回元数据 `modelUsage` 键为 `deepseek-v4.1-flash`；cliproxyapi `config.yaml`
存在该别名（629、686 行）；交互式包装器透传 `--model`（`~/.local/bin/claude-deepseek`）。
模型自报身份按规则不作判据，以配置与返回元数据为准。
**时长**：成功轮 775 秒（预算 15 分钟硬上限）。第一轮宽范围派发在 agent 循环内停滞
87 分钟无产出，按派发纪律终止后收窄范围重派；该事实本身记入回执（派发须写明
分钟预算与文件清单的教训）。

### 发现与处置

| 编号 | 级别 | 发现 | 处置 |
|---|---|---|---|
| F1 | 硬 | `verification-workflow.md:36`「空报告不得通过」与 `review-prompt.md` 「否则 COMPLETE」对同一份空报告要求相反信号 | 已修：规则明确「没有发现」须附实际尝试路径，空报告视同未验；prompt 同步收紧为未执行尝试即 BLOCKED |
| F2 | 软 | 反例转长期检查无执行者（Hacker 只读、实现者不默认扩张） | 已修：明确由实现者修复轮转换；Hacker 仅授权写入轮落盘且须再换手 |
| F3 | 硬 | 规则写 `dsv4.1f`，任何配置中不存在该名 | 已修：改为真实别名 `deepseek-v4.1-flash` |
| F4 | 硬 | `review-prompt.md` 缺规则要求的派发边界（入口/判据/允许操作/证据/预算） | 已修：新增 BOUNDARIES 块，逐条对应派发内容要求 |
| F5 | 硬 | `planner.ts` 只消费 1 bit 信号，审查报告随 sandbox 丢弃 | 已修：stdout 尾部 40 行随抛出错误与 COMPLETE 日志留存 |
| F6 | 低 | ADR 0036 引用 `planner.ts:292` 行号已漂移 | 已修：改符号名引用 |
| F7 | — | 逐字合规即可零验收（F1 的复合形态） | 随 F1 修复 |
| F8 | 结构性 | 审查轮仍持 GH token，审查提交可静默进合并 | 已修：`profileConfig(false)` 去 token；`review.commits > 0` 即抛错 |
| F9 | 结构性 | 单 issue 审查失败仅 console.error，GitHub 无痕迹 | 部分修：报告尾部进运行日志；GitHub 侧痕迹保留为已知边界（人工合并闸仍在） |

### 已核对一致的方面（Hacker 报告「已尝试」项）

- `{{BRANCH}}/{{ISSUE_NUMBER}}/{{ISSUE_TITLE}}` 全部由 `planner.ts` 传入；`<promise>` 标记
  在 `planner.ts`、`review-prompt.md`、`merge-prompt.md` 三方一致。
- 全部 10 个 prompt 入口、AGENTS/CLAUDE/README/afk-workflow/CODING_STANDARDS
  的相对路径均可解析；ADR 0220 引用的 0036/0030/0215 存在。
- review 超时 → 分支被排除合并，符合「超时不得自动通过」。

### 遗留未验（如实记录）

- `@ai-hero/sandcastle` 语义未从源码核实；`RunResult.stdout/.commits` 字段以主仓
  `node_modules` 的 `index.d.ts:591-601` 类型为准。未跑真实 ralph 轮次。
- 审查轮「只读」除 commits 检查外仍依赖提示词约束；sandbox 内的文件系统级只读
  不在本层实现。

## 未验范围

- 新分工在真实业务任务中的节时与检错收益：待后续任务按分工末段口径记录
  （有效反例、误报、总耗时、返工成本）。
- AFK 两条链路未在本轮真实跑批验证；planner.ts 改动经类型解析与逻辑核对，
  未经一次完整 ralph 运行。
- `claude-deepseek` 对 `deepseek-v4.1-flash` 的实际路由以 cliproxyapi 配置为准；
  模型自报身份不作判据。
