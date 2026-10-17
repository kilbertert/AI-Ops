# 规格树是意图侧的真值，门禁跑在 CI 而不是本机 git hook

日期：2026-10-17
状态：已采纳
影响版本：新增 `.spec/` 规格树（1 个根节点 + 7 个层节点 + 10 个规则/子系统节点）与
`.github/workflows/spec-tree.yml`；不改任何运行时代码

## 背景

本仓库的意图写得比多数仓库好：`README.md` 第三章有逐层的模块地图、六个设计推论、
六条向上依赖的逐条理由；各模块 docstring 里常有「曾被实现六次并已经漂移」这样的
第一手记录；`docs/adr/` 有 25 条决策记录；`GLOSSARY.md` 有术语表。

它们共同缺一样东西：**没有机制在代码改动后指出其中哪一条过期了**。

这不是假设。仓库自己记着这类缺陷的代价：`order_visibility.py` 的模块文档写着租户可见性
规则"曾被独立实现六次，并且已经漂移——同一订单在不同入口得到不同的可见性结论，
连失败原因都不同"。`async_job_lifecycle.py` 记着同一条生命周期规则曾有五份手写副本、
两种极性拼写、两套常量族。`answer_language.py` 记着语言守卫曾只覆盖一条回答面，
"于是一个中文泄漏被发现一次修一次，而它的兄弟们继续坏着"。

这三处的共同形态是：**同一条意图被复制到多处，副本随后各自漂移，而没有任何东西在漂移发生时
发出信号**。ADR 回答"为什么这样决定"，README 回答"模块怎么分、往哪读"，
两者都不回答"这段代码现在归哪条意图管、以及它是否还管得住"。

## 决策

引入 SpexCode 的 `.spec/` 规格树：`.spec/ai-ops/…/<node>/spec.md`，每个节点 frontmatter 的
`code:` 指向它治理的文件或具名单元（`path#Symbol` 即 anchor）。漂移由 git 现算：
自该 spec.md 最后一次提交以来，锚定单元的行区间是否被提交改动过。命中即 `anchor-drift`
错误并指名越线的那次提交。不存任何漂移数据库。

节点按 README 第三章的**既有分层**建，不另造分解：L0 契约 / L1 领域规则 / L2 身份与范围 /
L3 证据内核 / L4 推理与产品能力 / L5 持久化 / L6 服务边界，共 7 个层节点；
再把仓库里最贵的七条规则单独成节点并锚定到承载它们的单元——`order-visibility`、
`scope-context`、`tool-allowlist`、`job-lifecycle`、`metrics-redaction`、
`ops-analysis`、`tdengine-proxy`；外加三个有独立 ADR 的多模块子系统
（`dify-login`、`ops-agents`、`faq-catalog`），它们塞进父节点的 `related:` 会被藏起来。

采用时确立四条：

1. **以 `--pure` 引入。** 只新增 `.spec/`；不装 git hook、不改 `CLAUDE.md`/`AGENTS.md`、
   不生成 `.plugins/`。已验证三项为空：`git status CLAUDE.md AGENTS.md .claude` 无变化、
   `.git/hooks` 无新增、`.spec/` 未被 ignore。

2. **anchor 只落在承载契约的单元上。** `order_visibility.py` 锚 `#visible_orders` 而非整文件；
   `diagnostic_tools.py` 锚 `#DiagnosticToolExecutor`；`async_job_lifecycle.py` 锚 `#claim_guard`。
   文件级 anchor 会让任何无关改动都报漂移，噪音会训练人忽略这条检查。

3. **门禁跑在 CI，不接本机 `serverPolicy.chainedHooksPath`。** 阻断式 hook 技术上可接
   （在本机实测过：接上后服务器护栏仍优先、drift 门禁生效、逃生口可用），但它需要每台机器
   手工 `git config`，而 `spex init` 写入的内容不带机器可读标记，会被 `dev-policy-audit`
   判定为"仓库覆盖 `core.hooksPath`"并尝试修复。**一个会被别的工具悄悄撤销的护栏不是护栏。**

4. **CI 必须 `fetch-depth: 0`，版本钉 `0.7.0`。** 实测在真浅克隆（depth 1）下 lint 报
   `0 error 0 warning`，即便锚定单元已被改动。一个静默通过的检查比没有检查更糟。

## 被否掉的替代形态

**不引入规格树，靠 README 第三章 + docstring 维持现状。** 这是最强的替代，也是现状，
且本仓库的现状质量确实高——README 的模块地图细致到记录"为什么 `agent_contracts.py` 在 L0
而 `agent_engine.py` 在 L4：判据是入度而非直觉"，这种文字不是自动生成的。
它输在一个可测的事实上：**这些文档没有任何失效检测**。上节三处缺陷之所以能发生并活到
被写进 docstring 的程度，正是因为当时没有任何东西在改动时指出"这条意图的锚被碰了"。
规格树增加的不是第三份散文——spec 正文刻意比 README 短，多数的价值在 `code:` 那一行。

**把契约写进代码注释，而不是另一棵树。** 理由：注释就在代码旁边，改代码的人一定看得见，
不需要记得去看另一棵树。它输在两处：注释无法被 lint 成"这条意图归哪个文件管"的关系，
而这正是要检测的东西；且本仓库的注释纪律恰好走向反面——`.sandcastle/CODING_STANDARDS.md`
与各模块 docstring 的实践是把契约级意图写进模块文档，把本地坑写进 `@@@` 注释。
把契约塞进函数上方会被本仓库自己的规范判为层级错位。

**接 `serverPolicy.chainedHooksPath`，让本机提交就被拦。** 它确实能拦，反馈比 CI 早、
回路比 CI 短，这是它最强的理由。它输在可持续性：配置不在仓库里、需要每个 clone 重复、
且会被 `dev-policy-audit` 的修复路径撤销。代价是本机可以提交未消解的漂移、由 CI 在 PR 上拦
——反馈变晚，但不会失效。本仓库已有 25 条 ADR 与多条 CI 门禁的实践，
"晚一点但一定会发生"比"早一点但可能静默消失"更符合这里的既有取向。

**把规格树接到 `architecture-review.yml` 的定期评审里，而不是 PR 门禁。** 理由：
本仓库已有一个每工作日跑一次、按 issue 记账的架构评审面，把规格漂移交给它似乎顺理成章。
它输在**反馈对象不同**：定期评审产出的是 issue，需要人再决定；而漂移是"这次提交碰了契约单元
却没动契约"——它有一个明确的当下答案，且答案在提交那一刻最便宜。把它推给异步评审，
等于让一次本来三十秒能修的改动躺在 issue 队列里。

## 后果

**更容易的。** "这段代码归哪条意图管"从跨三份文档搜索变成读一个 frontmatter。
锚定单元被改动而 spec 未动时，lint 直接给越线提交号，以及
`git diff $(git log -1 --format=%H -- <spec.md>)..HEAD -- <file>` 这条读代码增量的命令。
根节点的架构图与 spec 树同源，`spex diagram check` 会验证图中每个框确实是该节点的直接子节点
——图不再是一份会单独过期的资产。

**更难的、以及被限制住的。** 新增一条维护义务：改动锚定单元时必须同步改 spec，
或用 `git commit --trailer "Spec-OK: <node>"` 记录"契约仍成立"的真实理由。
这条义务的强度完全取决于 anchor 选得准不准——选粗了退化成噪音，选细了（类型/接口）
重构时频繁误报，所以约定是锚行为承载单元。**本记录不主张这套机制能自动判断意图是否真的更新**：
`Spec-OK` 是一次诚实声明，不是一次验证；一个盲目的 ack 就是一句谎。

**一处已知的当前状态。** `spec-tree` 作业目前**不是必需检查**（必需检查为
`Workflow policy` + `verify`）。今天它会在 PR 上变红但不阻断合并。
把它提升为必需检查是一个独立决定，应先观察它在本仓库的误报率——
本仓库 85 个源文件、18 个节点，误报率还没有数据。

**本记录明确不主张的。** 它不主张 `.spec/` 取代 README 第三章、`docs/adr/`、`GLOSSARY.md`
或 `.sandcastle/REPO-MAP.md`。分工是：ADR 回答"为什么这样决定、否掉了什么"；
README 回答"模块怎么分、往哪读"；REPO-MAP 机械地报告文件系统与清单说了什么；
spec 节点回答"这段代码归属哪条意图、它是否还成立"。四者中只有最后一个是可锚定、可检测的。
