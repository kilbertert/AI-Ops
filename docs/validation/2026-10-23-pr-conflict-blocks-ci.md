## 2026-10-23 · PR 一冲突，CI 根本不派发 —— 改成「一个记录一个文件」

**症状**：并行开发时，PR 显示为 DIRTY（与 `main` 冲突），CI 一条都没跑。看起来像「CI
很慢」，实际是**从未启动**。

**根因（GitHub 官方行为，非缺陷）**：`pull_request` 事件的文档写明 —— *"Workflows will
not run on `pull_request` activity if the pull request has a merge conflict."* 冲突存在时
GitHub 不派发该工作流。实测 PR #643 的运行列表里只有 `pull_request_target` 的 agent 工作
流，`CI` 一次都没有。

**为什么冲突会这么多**：`docs/validation.md` 与 `docs/开发进度.md` 是追加式单文件，新条目
一律插在第 1 行。实测近 40 个已合并 PR：**23 个动了 `validation.md`、17 个动了
`开发进度.md`**。两个并行分支各自在同一个文件顶部插入，必然在同一处冲突。分支保护还是
strict（`strict_required_status_checks_policy: true`），每次合并都让其余所有 PR 落后。

**排除过的方案**：

| 方案 | 结论 |
|---|---|
| `.gitattributes` 的 `merge=union` | **本地有效、GitHub 无效**。GitHub 的 PR 冲突计算不读用户 gitattributes（社区讨论 #9288 自 2015 年起 Unanswered）。本地能自动合，GitHub 照标 DIRTY、照样不派发 CI。 |
| GitHub 官方 merge queue | 需要仓库属于**组织**（公开库）或 Enterprise Cloud（私有库）。本仓库是个人账号，用不了。且 merge queue 不解冲突，只把 PR 踢出队列。 |
| Stacked PR（Graphite 等） | 解决的是「一串有依赖的改动等评审」，不是「多个互不依赖的改动同时落地」。问题不同。 |

**采用的改法**：每条里程碑记录**新增**一个文件，路径是
`docs/validation/<YYYY-MM-DD>-<短名>.md` 与 `docs/开发进度/<YYYY-MM-DD>-<短名>.md`。
两个并行 PR 新增的是不同路径，冲突面归零。原有两个单文件冻结为只读归档。

**判据（可复现）**：

```bash
# 两个单文件不再被本次或后续里程碑编辑；新记录落在目录里
ls docs/validation/2026-10-23-*.md docs/开发进度/2026-10-23-*.md

# 归属检查已覆盖新目录（不是只读四个归档文件）
pytest tests/test_assistant_cancel_handoff.py -k evidence_entry -q
```

`tests/test_assistant_cancel_handoff.py` 的 `ATTRIBUTION_DOCS` 从「四个文件」改成
「四个文件 + 两个目录全量扫描」，并新增一条测试断言扫描**确实**到达了目录 ——
否则一个只返回归档文件的 helper 会让两条归属测试保持全绿而实际什么都没查。
