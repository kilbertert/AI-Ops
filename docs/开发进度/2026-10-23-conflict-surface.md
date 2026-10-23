## 2026-10-23 · 冲突来源收敛：生成物移出 + 可枚举清单

**目标/分支**：`chore/eliminate-conflict-sources`，基线 `3f03814`（#1016 / ADR 0097）。

### 一、纠正上一轮的结论

#1016 报告写「冲突面归零」，实际只处理了两个证据日志。对 105 个远程分支完整枚举后，
真实冲突分布前六名是：`validation.md` 65、`开发进度.md` 60、**`REPO-MAP.md` 34**、
`.afk-bootstrap.json` 15、`current-delivery-state.md` 12、`images.lock.json` 8。
**前三名里有两个是生成物。**

### 二、做了什么

1. **`.sandcastle/REPO-MAP.md` 移出版本控制**（`.gitignore` + 按需生成）。
   `repo-map.check.mjs` 不再比对提交副本（已无副本），改为断言**确定性**：同一棵树
   两次渲染逐字节相同。5 个 agent prompt 改成「先 `node .sandcastle/repo-map.mjs`
   再读」。两个 AFK workflow 里为新鲜度门禁而存在的「重新生成」步骤一并删除。
2. **`.sandcastle/conflict-surface.mjs`（新增）**：枚举每个远程分支的冲突路径并给出
   处置。`--self-check` 验证三张表互不重叠且每项都有理由；`--check` 只为 `removed`
   项变红。已接进 `AFK Policy`（必需检查）。
3. **ADR 0098** 记录第一性原理与四个被否的备选路线。

### 三、判据

```bash
node .sandcastle/conflict-surface.mjs --self-check   # 表自检
node .sandcastle/conflict-surface.mjs                # 末尾须为 0 prose hotspots
node .sandcastle/repo-map.check.mjs                  # deterministic
```

当前：`1 removed · 2 exclusive · 29 阈值以下 · 6 serial · 60 source`，
**0 个 hotspot**。

### 四、已知边界

- **第三类冲突不会消失**：60 个源码模块 + 6 个 `serial` 散文文件（`acceptance.feature`、
  `qa-plan.md`、`env-41-runbook.md`、`GLOSSARY.md`、两个组件 README/ADR）仍会在并行
  分支上冲突。它们的写者确实改同一处，靠调度控制，不靠文件布局。
- `CHANGELOG.md` 可从 Conventional Commits 重建却仍手改入库，已记进清单，是下一个
  `removed` 候选。
- 清单里的 `REPO-MAP.md` 项要等**本 PR 合并后**才会消失：存量分支仍带着被跟踪的副本。
  `--check` 因此只在合并后的分支上才会转绿 —— 这是它的设计，不是缺陷。
