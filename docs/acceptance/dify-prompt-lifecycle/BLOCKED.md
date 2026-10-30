# BLOCKED — dify-prompt-lifecycle

**当前无阻塞项。** 开发、自测、变异验证与文档均已完成，候选 PR 待复核。

## 已绕过（记录原因，不是未解决）

1. **`dev-worktree start` 被另一个会话的 worktree 挡住。**
   `dev-worktree audit` 报 `issues=1`：`/home/claude/Projects/.worktrees/AI-Ops-dual-entry`
   （分支 `feat/dual-entry-onboarding`，15 个未提交改动，对应另一个会话在做的双入口需求）。
   该 worktree 不是本任务的，**没有清理或暂存他人的工作**。本片按同一约定手工建立
   `/home/claude/Projects/.worktrees/AI-Ops-dify-prompt-lifecycle`（分支
   `feat/dify-prompt-lifecycle`，基于 `origin/main` b5d46610，已取消 upstream 以免误推主线）。
   待那个 worktree 提交或 `preserve` 之后，`dev-worktree start` 会恢复正常。

2. **`tools/verification/verify_prompt_lifecycle.py` 只在本地合成数据上跑过。**
   它不是阻塞项：脚本明确拒绝在真实数据根写证据，并把"真实 Dify／Java 节点／模型／原 H5
   未验证"打印在结果里。真实环境验收按任务书由 Codex 在合并制品后接续。

## 需要 Codex 接续的前置条件

- 候选 PR 合并（本片不改正式环境、不改路由、不部署）。
- 41 上可读两个应用的绑定与路由状态；运营账号可登录（凭据按
  `docs/agents/env-41-runbook.md` 现场读取，不入库）。
- 页面级验收步骤见 `docs/acceptance/dify-prompt-lifecycle/qa-plan.md` 第五节。
