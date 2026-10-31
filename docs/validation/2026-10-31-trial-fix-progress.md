# 受管预览修复：进度与一个依赖（2026-10-31）

按 Codex 复核四项决定的第 3 项实施。**代码改完、测试绿，但推送被一个在途工作树挡住。**

## 已完成的修复（本地提交 `c59d0bf0`，未推送）

1. **`data_kind` 记录 = 实际请求**
   `ConsumerTrial` 原来记录写 `status`、请求传 `knowledge`；
   现改为同一个值。`dify_run_store` 白名单接受 `knowledge`。
   → Codex 的复现脚本从红转绿；固化为仓内回归并做了变异验证（撤修复即红）。

2. **失败原因可读**
   后端早已返回 `error_message`，前端 `aiops-workflow-managed.tsx` 却只读
   `result.report.summary`，失败一律显示通用文案。现优先展示 `error_message`。
   → 新增回归断言失败时 `error_message` 非空且不含内部异常文本。

验证：`tests/consumer/test_managed_trial.py` 9 passed；全量 **3821 passed / 9 skipped / 0 failed**；
formatter/静态通过。

## 阻塞：`deploy/m2-live-03/images.lock.json` 被在途工作树占用

`dev-worktree guard-overlap` 退出 3：

```
overlap worktree=/home/claude/Projects/.worktrees/AI-Ops-dual-entry-onboarding-review
        branch=fix/dual-entry-onboarding-review path=deploy/m2-live-03/images.lock.json
```

该 worktree：`dirty=6`、**7 个活跃进程**、正在改同一个锁文件（`built_at=16:36:02`，就在刚才），
且**尚无 PR**。我不能动它。

我改了 `src/`，仓库规则要求同步重建 `source_sha256`，否则 `test_m2_live03_deploy` 会红。
先移除锁文件可以让 guard 放行，但 CI 必红——那只是把冲突推后。

## 顺带发现（已处理）

执行 `git stash pop` 时误弹了别人的 stash（`README 入链补丁`），造成 3 个文件冲突标记。
已备份到 `/tmp/rescue-*` 并 `git reset` + `checkout` 恢复；我的提交未受影响，stash 列表未丢失。

## 需要决断

- **等**：那个 worktree 落盘后，我重建摘要再推（推荐；不碰他人工作）。
- **或**：由你协调该 worktree 归属者先提交/暂存，我再推。
