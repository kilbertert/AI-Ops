# 97. 里程碑记录一条一个文件，落后与冲突由工作流自动派发

日期：2026-10-23
状态：已采纳
影响版本：`AGENTS.md`（里程碑同步）、`README.md`（文档与证据纪律）、
`tests/test_assistant_cancel_handoff.py`（`ATTRIBUTION_DIRS`）、
`.github/workflows/sync-open-prs.yml`（新增）

## 背景：并行 agent 撞在同一行，而 GitHub 对有冲突的 PR 不跑 CI

多个 Claude / Codex 会话并行开发时，PR 经常处于 DIRTY（与 `main` 冲突）。观察到
的表象是「CI 很慢」，实际是**从未启动**。

两条事实叠在一起才造成这个结果：

1. **GitHub 的规则**：`pull_request` 事件的官方文档写明 —— *"Workflows will not run on
   `pull_request` activity if the pull request has a merge conflict."* 实测 PR #643 的运行
   列表里一条 `CI` 都没有，只有 `pull_request_target` 的 agent 工作流。
2. **本仓库文档的形状**：`docs/validation.md` 与 `docs/开发进度.md` 是追加式单文件，每条
   新记录都插在**第 1 行**。实测近 40 个已合并 PR：23 个动了 `validation.md`、17 个动了
   `开发进度.md`。两个并行分支在同一个位置插入，冲突是必然的，不是偶发。

叠加 strict 分支保护（`strict_required_status_checks_policy: true`）：一天 32 次合并，
每次合并都让其余所有开着的 PR 落后。

## 决定

### 一、文档：一个记录一个文件

每条里程碑**新增** `docs/validation/<YYYY-MM-DD>-<短名>.md` 与
`docs/开发进度/<YYYY-MM-DD>-<短名>.md`。两个单文件冻结为只读归档。

并行的 PR 新增的是**不同路径**，冲突面归零。

### 二、落后与冲突：自动派发，不新写合并逻辑

`sync-open-prs.yml`（`push: main` + 每日定时）只做分类：

| 状态 | 动作 |
|---|---|
| `BEHIND` | `gh pr update-branch --rebase` |
| `DIRTY` | 加 `agent:update-branch`，交给已有的 `agent-update-branch.yml` |

## 备选方案与取舍（被否的路线）

这些是后来的人会再次想到的，所以写下来：

- **`.gitattributes` 的 `merge=union`**。本地 git 支持（实测本地 merge 与 rebase 都能
  自动合并且顺序正确），但 **GitHub 的 PR 冲突计算不读用户 gitattributes** —— 社区讨论
  #9288 自 2015 年起一直是 Unanswered。结果是：本地看起来已经解决了，GitHub 照标 DIRTY、
  照样不派发 CI。这个方案的危险不在于无效，而在于**它看起来有效**。
- **GitHub 官方 merge queue**。需要仓库属于组织（公开库）或 Enterprise Cloud（私有库）。
  本仓库是个人账号（`owner.type = User`，无组织）。而且它**不解冲突**，只把 PR 踢出队列 ——
  它治的是「每次合并都让所有人落后」，不是「两个 PR 改同一行」。
- **Stacked PR（Graphite / ghstack）**。解决的是「一串有依赖的改动等评审」，本仓库的问题
  是「多个互不依赖的改动同时落地」。问题不同，工具对不上。
- **在 `sync-open-prs.yml` 里自己写 rebase + 冲突解决**。既有
  `agent-update-branch.yml` 带完整信任边界（controller/candidate/delivery 三次 checkout、
  只读 token 读候选、`AGENT_PAT` 才推送、结果过 policy check）。第二套更简单的实现只会是
  第二处能出错的地方，而且**更简单的那套正是会漂移的那套**。

## 后果

- 归属检查（`test_assistant_cancel_handoff.py`）从「四个文件」扩到「四文件 + 两目录全量」，
  并新增一条断言**扫描确实到达了目录** —— 否则一个只返回归档的 helper 会让两条归属测试
  保持全绿而实际什么都没查。这条守卫是必要的：目录扫描的失败模式是静默空转。
- 自动 rebase 会重写任务分支，因此只对**无 assignee** 的 PR 执行；仓库规则是不为解决
  漂移而 force-push 已发布的任务分支。
- 标签必须用 `AGENT_PAT` 打：`GITHUB_TOKEN` 加的标签不触发下游工作流
  （`agent-promote-queued.yml` 已为此踩过同一个坑）。
- `docs/validation/` 原本就混放 `.json` / `.py` 证据产物；新记录的 `.md` 与它们共处是
  既成约定，归属检查按 `rglob("*.md")` 只读 Markdown。
- 这个改动**不会**让冲突消失 —— 它让冲突的**来源**（并行在文件顶部插入）消失，并让剩下的
  落后/冲突自动被处理。如果将来又引入一个高频改写的单文件（配置索引、清单），同样的
  问题会回来。
