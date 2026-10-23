## 2026-10-23 · 发布步骤在 runner 上静默跳过

**目标/分支**：`fix/release-step-writes-where-it-looks`，基线 `d535c9e`（#1032）。

### 一、这条链第一次真正执行，就暴露了缺陷

#1032 修好依赖漂移门后，CD 首次跑过 Deploy（**部署成功**），却停在 `Publish release
notes`：`cp: cannot stat 'CHANGELOG.md'`，而前一行 `dev-changelog .` 返回 0、日志无输出。

### 二、根因

`dev-changelog` 只为声明了 `serverPolicy.publishesVersions` 的仓库渲染，而那条声明是
**local git config、从不入库**。runner 的 checkout 是全新 clone，没有它 ——
`skip … not declared`，**exit 0**，不写文件。

**`skip` 与 `wrote` 都是 0。** 只有文件能区分。

### 三、改法

- 在发布步骤里显式声明两条 local config
- 用 `git rev-parse --show-toplevel` 取绝对根，不用 `.`
- **不再吞输出**：`dev-changelog` 自己会打印 `wrote <path>`，那是唯一证据
- `[ ! -s … ]` 断言文件存在，并把最可能的原因直接写进错误

### 四、验证

全新 clone、无声明、detached HEAD 复现：

| | 结果 |
|---|---|
| 修改前 | `skip … not declared`，rc=0，无文件 |
| 修改后 | `wrote …`，文件存在，守卫通过 |

### 五、待验证

**这条链仍未在生产跑通一次。** 本次改动需要一次新的 `src/**` 合并才会再次触发 CD。
只有看到 GitHub Release 真的建出来，才能说这条链闭环。
