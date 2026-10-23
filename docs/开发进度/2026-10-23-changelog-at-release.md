## 2026-10-23 · CHANGELOG 生成时机挪到发布

**目标/分支**：`chore/changelog-at-release-time`，基线 `7b5d6fe`（#1025 / ADR 0100）。

### 一、这一片解的是什么

ADR 0100 把 `CHANGELOG.md` 拆成「生成物 + 人工发布说明」，但如实记下它**仍会冲突**：
生成器输入是 commit 范围，main 每前进一次就过期；任何 PR 要通过门禁都得重新生成，
于是每个 PR 都写它。冲突从「两人手写」变成「两分支重生成」，频率几乎没变。

### 二、改法

| 改动 | 文件 |
|---|---|
| 移出版本控制 | `.gitignore`、`CHANGELOG.md` |
| 部署成功后生成并发布为 GitHub Release | `.github/workflows/cd.yml` |
| 冲突面改为 `removed` | `.sandcastle/conflict-surface.mjs` |
| 审计支持 `serverPolicy.changelogAtRelease` | 服务端 release unit #72（已合并并安装） |
| 部署文档同步（失败分诊、文件去向） | `README.md`、`docs/agents/env-41-runbook.md` |

### 三、为什么是 Release 而不是提交回 main

查过规则集：`bypass_actors` 为空、`pull_request` 规则覆盖所有更新 → bot 推 main 必被拒。
开 bypass 等于为一份 changelog 在「未经评审的代码不得进入 main」上打洞。

### 四、判据

```bash
node .sandcastle/conflict-surface.mjs --check   # 被跟踪 → 1，未跟踪 → 0
```

### 五、已知边界

- 本仓从未打过版本 tag，`dev-changelog` 按 `v?<major>.<minor>.<patch>` 分节，因此备注只有
  一个 `Unreleased` 节：回答「哪些改动已落地」，不回答「上次部署里有什么」。策略文件对
  这种情形已有明文称为 stated limit。**不顺手发明版本号** —— 那是一次发布包含什么的决定。
- `Publish release notes` 与 `Deploy` 是两次独立的失败面。前者红了**不代表生产有问题**，
  运维文档里按步骤分别给了处置；把两者混为一谈会导致不必要地回滚一次成功的部署。
