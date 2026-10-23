# 101. CHANGELOG 在发布时生成，Pull Request 不再触碰它

日期：2026-10-23
状态：已采纳
影响版本：`.gitignore`、`CHANGELOG.md`（移出版本控制）、`.github/workflows/cd.yml`
（新增发布步骤、`contents: write`）、`.sandcastle/conflict-surface.mjs`、
`serverPolicy.changelogAtRelease=true`（本机声明）

## 背景：ADR 0100 留下的那个未解项

ADR 0100 把 `CHANGELOG.md` 拆成「生成物 + 人工发布说明」，并如实写下它**仍会冲突**：

- 生成器的输入是 commit 范围，`main` 每前进一次它就过期；
- 任何 PR 要通过门禁就得重新生成 → 每个 PR 都写它；
- 实测两侧在同位置追加，`git merge` 会冲突。

拆名字解决的是「两份内容混在一个文件里」，没有解决「每个分支都写同一个文件」。

## 决定：生成时机从「每个 PR」挪到「每次发布」

`CHANGELOG.md` 加入 `.gitignore`、移出版本控制。`cd.yml` 部署成功之后，在
self-hosted runner 上运行 `dev-changelog .`，把结果作为 **GitHub Release** 发布。

于是没有任何 PR 写它，没有任何分支能因为它冲突。

## 为什么是 GitHub Release，而不是提交回 main

第一版写成「部署后提交 CHANGELOG 并推回 main」。动手前查了规则集：

```
$ gh api repos/kilbertert/AI-Ops/rulesets/23760870 --jq '{bypass:.bypass_actors, rules:[.rules[].type]}'
{"bypass":[],"rules":["deletion","non_fast_forward","pull_request","required_status_checks"]}
```

**`bypass_actors` 为空，且 `pull_request` 规则覆盖所有更新。** bot 推 main 会被拒。
要推就得给规则集开 bypass —— 那是为了一个 changelog 提交，在「未经评审的代码不得进入
main」这条边界上打洞。不该由这一片决定。

发布为 Release 同样满足「消费方拿得到」，且完全在 gate 之外。

## 已知限制（继承自既有策略，不是本文引入的）

`dev-changelog` 按 `v?<major>.<minor>.<patch>` tag 分节。本仓从未打过这样的 tag
（`git tag` 只有一条非版本归档 tag；`pyproject.toml` 的 version 一直是 0.1.0），所以
生成结果只有一个 `Unreleased` 节。

这意味着 Release 备注回答「哪些改动已经落地」，**不回答「上一次部署里有什么」**。策略
文件对这种情况已有明文（*a deployed service that creates no tags renders a single
`Unreleased` section … That is a real and stated limit*）。

本文**不**顺手发明版本号：给发布编号是「一次发布包含什么」的决定，不该由流水线代做。
等真正建立打 tag 的实践后，`gh release create --target` 已指向被部署的 commit，同一段
代码无需改动就会开始产出按版本分节的备注。

## 备选方案与取舍（被否的路线）

- **提交回 main（CD 直接 push）。** 被否：规则集不允许。为此开 bypass 是拿评审边界换
  一个文件。
- **保留入库，PR 侧继续重新生成。** 被否：这正是 ADR 0100 记录下来的、频率几乎不变的
  冲突；换名字不改变它。
- **在 PR 里生成、提交，再由 CD 校验。** 被否：把同一个冲突原样留在 PR 侧。
- **顺手把版本号推到 1.0.0 并按版本打 tag。** 被否：版本号的含义是「这个发布包含
  什么」，本仓的版本从未被当作发布边界（`pyproject` 停在 0.1.0）；流水线代选一个数字，
  会让 tag 既不反映决定也不再可信。

## 后果

- 冲突面上 `CHANGELOG.md` 从「有理由的共享」变成 `removed`；`--check` 在它被重新跟踪
  时失败（实测：`git add -f` 后 exit 1，移除后 exit 0）。
- `dev-policy-audit` 新增 `serverPolicy.changelogAtRelease`：声明为 true 时，文件不存在
  是正确的、不再告警。声明必须显式 —— 「不存在」和「忘了生成」在磁盘上是同一串字节。
- `cd.yml` 的 `permissions` 从 `contents: read` 变为 `contents: write`，只为创建
  Release；部署本身仍走 SSH 自己的密钥，不经这个 token。
- 发布步骤放在 Deploy **之后**：备注描述一个实际没发生的发布，比没有备注更糟。
