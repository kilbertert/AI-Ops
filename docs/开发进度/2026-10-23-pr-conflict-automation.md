## 2026-10-23 · 冲突自动化：文档拆成一条一文件 + 落后/冲突自动派发

**目标/分支**：`chore/sync-pr-conflict-automation`，基线 `0d41b6b`（#1014）。

### 一、文档拆成「一个记录一个文件」（根因）

`docs/validation.md` 与 `docs/开发进度.md` 改为**只读归档**；此后每条里程碑**新增**
`docs/validation/<YYYY-MM-DD>-<短名>.md` 与 `docs/开发进度/<YYYY-MM-DD>-<短名>.md`。

依据与实测见同批的 `docs/validation/2026-10-23-pr-conflict-blocks-ci.md`：近 40 个已合并
PR 中 23 个动 `validation.md`、17 个动 `开发进度.md`；插入位置都在第 1 行，并行分支必然
冲突；而 GitHub 对**有冲突的 PR 根本不派发 `pull_request` 工作流**。

### 二、落后与冲突自动派发（不新写合并逻辑）

新增 `.github/workflows/sync-open-prs.yml`：`push: main` 与每日定时触发，扫开着的
非草稿 PR：

- `BEHIND`（无冲突、仅落后）→ `gh pr update-branch --rebase`，省一次 agent 运行；
- `DIRTY`（真冲突）→ 加 `agent:update-branch` 标签，交给**已有的**
  `agent-update-branch.yml`（它会起 agent 解冲突、过 policy check、用 `AGENT_PAT` 推送）。

关键取舍：

- **不新写冲突解决逻辑。** 仓库里已经有一套带信任边界（controller/candidate/delivery 三个
  checkout、只读 token 读候选、`AGENT_PAT` 才推送）的实现，重复一套只会多一处能出错的地方。
- **标签必须用 `AGENT_PAT` 打。** 用 `GITHUB_TOKEN` 加的标签不会触发下游工作流 ——
  `agent-promote-queued.yml` 已为此踩过同一个坑并留了注释。
- **跳过草稿。** 草稿 PR 的冲突不阻塞任何人，交给 agent 解是纯浪费。
- **跑在 GitHub-hosted。** 这个工作流是「守望者」—— self-hosted runner 掉线时恰恰最需要
  它，共用那台机器会把要修的静默原样复制一遍（`cd-watch.yml` 已为同一原因做过这个选择）。

### 三、已知边界

- `docs/validation/` 原本就混放着 `.json`/`.py` 证据产物；新记录的 `.md` 与它们共处
  同一目录是既成约定，归属检查按 `rglob("*.md")` 只读 Markdown，不会误读产物。
- 自动 rebase 会重写任务分支；仓库规则是「不为解决漂移而 force-push 已发布任务分支」，
  因此仅在 PR **没有** assignee（即无人在本地持有该分支）且**未**处于 `agent:in-progress`
  时才动它。
- 冲突自动解仍受 `agent-update-branch` 原本的 token 与 policy 边界约束，本改动没有放宽任何
  一条。

### 验证

- `pytest tests/test_assistant_cancel_handoff.py -q`（含新增的目录覆盖断言）
- `node .sandcastle/policy-check.mjs workflows`（新工作流需满足既有工作流策略）
- `dev-worktree audit`
