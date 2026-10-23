# 「PR 冲突时不派发 CI」—— 以及怎么在 30 秒内证伪其他解释

**症状**：push 成功、远程 HEAD 也是新 sha，但 `gh pr checks` 永远只显示上一次
sha 的结果。等十分钟、二十分钟、半小时都一样。看起来像 "Actions 挂了" 或 "任务
排队中"。

**实测（2026-10-23，PR #977）**：

| 时刻 | 动作 | 结果 |
|---|---|---|
| 01:02 | 推 `57a0e0f` | CI 跑了，**2m0s success** |
| 01:06 | 推 `07e2a0f` | **0 run** |
| 01:33 | 推空提交 `da88eb0` | **0 run**（空提交也没用） |

同一时段另一个分支 `docs/consumer-order-chat-delivery` 在 01:12 正常触发了 CI
—— 所以不是仓库级故障，是这个 PR 本身。

根因：`gh pr view --json mergeStateStatus` 返回 **`DIRTY`**。期间 main 前进了三个
提交（#978/#979/#980），与本 PR 在 `deploy/m2-live-03/images.lock.json` 和
`docs/validation.md` 上冲突。**GitHub 不为无法构建合并结果的 PR 派发新的
`pull_request` workflow run。**

rebase 到最新 main 并解决冲突后，`mergeStateStatus` 变成 `BLOCKED`（等 check，
正常），CI 立即正常启动。

## 判别顺序：先证明"没跑"，再猜"为什么慢"

上一轮的失败是把「没看见结果」直接解释成「平台延迟」——那是猜测。正确的顺序是：

```bash
# 1. 本地和远程是不是同一个 sha（排除 push 没成功）
git rev-parse HEAD
git rev-parse origin/<branch>

# 2. 这个 sha 上到底有没有 run —— 直接问平台，不看 PR 页面的聚合
gh api "repos/{owner}/{repo}/actions/runs?head_sha=<sha>" | jq '.total_count'
gh api "repos/{owner}/{repo}/commits/<sha>/check-runs" | jq '.total_count'

# 3. 有 run ⇒ 才轮到"慢"；0 run ⇒ 是"没派发"
gh pr view <pr> --json mergeStateStatus,isDraft,headRefOid
```

`total_count: 0` 就把问题从「CI 慢」改写成「CI 没被派发」，两个完全不同的问题。

## 已知的四种「0 run」成因

按遇到频率排：

| `mergeStateStatus` / 状态 | 含义 | 处置 |
|---|---|---|
| `DIRTY` | 与 base 冲突，无法构建合并结果 | rebase 到最新 base，解决冲突后 push |
| PR 是 draft 且 `isDraft: true` | `ci.yml` 的 job 级 guard 跳过 draft | 标记 ready（`ready_for_review` 已在触发列表里） |
| `ci.yml` 的 `concurrency` 把上一次 run 取消且未重新入队 | 竞态 | 再 push 一次 |
| 仓库 Actions 被停用 | 全局，所有分支都 0 run | `gh api repos/{o}/{r}/actions/permissions` |

**跨分支对照是最快的分流器**：`gh api "repos/{o}/{r}/actions/runs?per_page=5"`
看别的分支有没有在跑。别人的在跑 ⇒ 排除仓库级故障，只剩本 PR 的 `mergeStateStatus`。

## 为什么空提交不是修复

`git commit --allow-empty` 触发的是同一个 `synchronize` 事件，撞的是同一个条件。
冲突没解决时它和普通 push 一样不派发 —— 我上一轮正是先推了空提交，白等 26 分钟。
**空提交只在"有 run 但被 concurrency 吞掉"时有用，不能用来排查冲突。**

## 相关

- `.github/workflows/ci.yml` 头部注释：为什么只保留 `pull_request` 触发（squash
  merge 让 PR head 与合并树 byte-identical，`push: main` 是重复测同一份字节）。
- 同一份注释里对 `ready_for_review` 的说明：draft 被跳过会记录 `skipped`，而
  `skipped` **满足**必备检查 —— 所以草稿转正必须能触发。
