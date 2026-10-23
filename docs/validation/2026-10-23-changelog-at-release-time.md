## 2026-10-23 · CHANGELOG 改为发布时生成

> **未完成业务验收。** 这一篇记的是发布链与文件归属：判据是规则集查询、生成器的
> 实测输出与本地可复现的门禁。它**不**包含工程师确认的真实故障案例，也**不**是
> 用户侧业务验收。

### 一、解决了 ADR 0100 留下的未解项

ADR 0100 拆开了「生成物」与「人工发布说明」，但如实记下 `CHANGELOG.md` **仍会冲突**：
生成器的输入是 commit 范围，main 每前进一次它就过期，每个 PR 都得重新生成。

### 二、改法

- `CHANGELOG.md` 加入 `.gitignore`、移出版本控制
- `cd.yml` 在 Deploy 成功之后运行 `dev-changelog .`，作为 **GitHub Release** 发布
- 本机声明 `serverPolicy.changelogAtRelease=true`，审计据此不再要求该文件存在

### 三、为什么不是「提交回 main」

查过规则集，不是推测：

```
$ gh api repos/kilbertert/AI-Ops/rulesets/23760870 \
    --jq '{bypass:.bypass_actors, rules:[.rules[].type]}'
{"bypass":[],"rules":["deletion","non_fast_forward","pull_request","required_status_checks"]}
```

`bypass_actors` 为空、`pull_request` 覆盖所有更新 → bot 推 main 必被拒。开 bypass 等于
为一份 changelog 在「未经评审的代码不得进入 main」上打洞。

### 四、判据（可复现）

```bash
node .sandcastle/conflict-surface.mjs --check
```

实测：

```
CHANGELOG.md 被跟踪   → exit 1
CHANGELOG.md 未被跟踪 → exit 0
```

`--self-check` 同时保证它落在 `STRUCTURAL` 而非 `EXPLAINED_SHARED` —— 本次编辑中我一度
把它放错表，门禁因此静默失效，自检发现了它。

### 五、已知限制（刻意的）

`dev-changelog` 按 `v?<major>.<minor>.<patch>` 分节，而本仓从未打过版本 tag
（`git tag` 只有一条归档 tag；`pyproject.toml` 的 version 一直是 0.1.0）。所以生成的
备注只有一个 `Unreleased` 节：**回答「哪些改动已落地」，不回答「上次部署里有什么」。**

策略文件对这种情形已有明文称为 *a real and stated limit*。

**不顺手发明版本号**：给发布编号是「一次发布包含什么」的决定。等真正建立打 tag 的实践
后，`gh release create --target` 已指向被部署的 commit，同一段代码会自动开始按版本分节。
