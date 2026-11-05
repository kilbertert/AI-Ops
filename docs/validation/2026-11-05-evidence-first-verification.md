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
87 分钟无产出，按派发纪律终止后收窄范围重派。**成本按全量计**：87 分钟空转 + 775 秒
成功轮 + F1–F9 修复轮 + 后续两轮实战复验（#1384），不是只算 775 秒；该事实本身记入
回执（派发须写明分钟预算与文件清单的教训）。

### 发现与处置

| 编号 | 级别 | 发现 | 处置 |
|---|---|---|---|
| F1 | 硬 | `verification-workflow.md:36`「空报告不得通过」与 `review-prompt.md` 「否则 COMPLETE」对同一份空报告要求相反信号 | 已修：规则明确「没有发现」须附实际尝试路径，空报告视同未验；prompt 同步收紧为未执行尝试即 BLOCKED |
| F2 | 软 | 反例转长期检查无执行者（Hacker 只读、实现者不默认扩张） | 已修：明确由实现者修复轮转换；Hacker 仅授权写入轮落盘且须再换手 |
| F3 | 硬 | 规则写 `dsv4.1f`，任何配置中不存在该名 | 已修：改为真实别名 `deepseek-v4.1-flash` |
| F4 | 硬 | `review-prompt.md` 缺规则要求的派发边界（入口/判据/允许操作/证据/预算） | 已修：新增 BOUNDARIES 块，逐条对应派发内容要求 |
| F5 | 硬 | `planner.ts` 只消费 1 bit 信号，审查报告随 sandbox 丢弃 | **误报（前提错）**：SDK 默认把每轮 agent 输出写入宿主 `.sandcastle/logs/`（`agent-*-reviewer-*.log`），报告并未随容器丢失；stdout 尾部留存仍保留为冗余路径 |
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

## 外部复审（2026-11-05 复审轮，Codex 外部会话，结论：暂不通过）

> 订正：本节早前把复审执行者误记为 `deepseek-v4.1-flash`；实际为本会话外的 Codex。
> 处置表中的修复为第一轮复审修复（PR #1397，合并 224ac847）。

复审对合并后终版提出四项，均已按复审判据修复（实现见修复提交）：

| 发现 | 级别 | 处置 |
|---|---|---|
| 派发把真实凭据带进模型上下文（Hacker 读代理配置/settings） | P1 | 规则与派发模板改为「派发者给脱敏结论、不给路径」，点名禁读凭据路径；受影响凭据轮换由宿主执行（范围含 cliproxyapi 上游 key 与 settings.deepseek.json 认证令牌，见交付说明） |
| `profileConfig(false)` 不去 token（进程环境补回 + 容器 env 不可擦） | P1 | 显式空值优先于环境变量；审查改为第二只无凭据沙箱（fork 实现分支），回执「已修」表述已订正 |
| SDK `completionSignal` 全文 `includes` 取首信号，引用标记骗过闸门 | P1 | 全部标记须一致（冲突/缺失判失败）+ 报告非空；merger 同闸 |
| `~~~still-inside-code` 被当闭合围栏（CommonMark 仅允许尾部空白） | P2 | 闭合判定收紧 + 反例 fixture 保存（`tildeFakeClose`） |

复审同时指出 F5 前提不成立（已更正为误报）、本回执缺 #1377 终版独立复验记录、
缺规定的 `docs/开发进度/` 文件（已补）、首轮 87 分钟空转须计入成本（已计）。

**终版独立复验（#1397 修复版）终态：未执行。** 两次派发（900s / 600s 硬上限）均因
上游 deepseek-v4.1-flash 流式停滞零产出被 timeout 终结——非「模型认可」亦非
「未发现问题」，按未执行计；任务文件留存 `/tmp/hack-review-hardening.md`。

## 外部复审第二轮（2026-11-05，Codex 外部会话，结论：仍暂不通过）

复审核对合并提交 224ac847，确认「COMPLETE 与 BLOCKED 冲突」与
`~~~still-inside-code` 两反例已被拦截，另提出四项新发现：

| 发现 | 级别 | 处置 |
|---|---|---|
| `strictSignal` 仍接受内联引用标记（示例句即放行） | P1 | 改为位置判定：仅独占整行的标记计数，且最后一非空行必须是独立裁决；两个 prompt 写明「末行独立裁决」合同；R1/R2 两轮 PoC 九例断言全过 |
| `.sandcastle/.env` 的 `GH_TOKEN`/`GITHUB_TOKEN` 经 resolvedEnv 仍可注入审查容器 | P1 | `credentialless` 意图时把两键钉空（sandbox env 优先于 resolvedEnv）+ 容器创建后探测实际环境断言（哨兵输出，密钥永不落日志） |
| 审查分支决定论重名 → SDK 复用已有 ref 忽略 baseBranch，验旧码删错分支 | P1 | `review/N-Date.now()` 唯一名 + `git rev-parse` 核对审查 ref==候选 tip + 清理只针对本 run 创建的分支 |
| 围栏闭合尾部 `\s*` 接受 U+00A0（NBSP 伪闭合再次放行） | P2 | 尾部限定 `[ \t]*`，前导缩进收窄 `[ ]{0,3}`（CommonMark 缩进只认空格）；`tildeNbspClose` 反例 fixture |

复审要求的回执订正：轮换范围扩至 `settings.deepseek.json` 认证令牌（不只上游
key）；「模型可用性不构成合并检查」不等于验收免除——终版独立复验须由独立执行者
实际完成后方记完成；复审执行者标签订正如上。
- AFK 两条链路未在本轮真实跑批验证；planner.ts 改动经类型解析与逻辑核对，
  未经一次完整 ralph 运行。
- `claude-deepseek` 对 `deepseek-v4.1-flash` 的实际路由以 cliproxyapi 配置为准；
  模型自报身份不作判据。
