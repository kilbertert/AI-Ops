## 2026-10-23 · 依赖漂移门改比「依赖声明」而非文件字节

> **未完成业务验收。** 这一篇记的是部署门的判据：证据来自两侧 manifest 的实际比对。
> 它**不**包含工程师确认的真实故障案例，也**不**是用户侧业务验收。

### 一、症状

CD 连续两次失败，都停在依赖漂移检查，报「目标 commit 的 pyproject.toml 与 41 上的
不一致」。部署因此从未到达新加的发布步骤。

### 二、根因：判据比的是字节，问的是语义

两侧 `pyproject.toml` 的 sha256 不同，但用 TOML 解析器逐项比对：

```
运行期 dependencies        相同
optional-dependencies      相同
dependency-groups.dev      相同
requires-python            相同
```

差异只有**注释**与 pytest 的 `addopts`（`-n auto` → `-n 4`，那是 #1023 的测试并发修复）。
`uv.lock` 两侧 sha256 **完全一致**。

`deploy-41.sh` 的注释写明了它想回答的问题：「restart 会不会因为缺包失败」。注释回答不了
这个问题，而按字节比会把每一次注释修改都判成依赖漂移 —— 于是 CD 被永久卡住，直到有人
按人工流程去生产机上动依赖环境。

### 三、改法

`pyproject.toml` 改比**依赖声明**（`dependencies` / `optional-dependencies` /
`dependency-groups` / `requires-python`，两侧各用 `tomllib` 解析后规范化成 JSON 比较）。
`uv.lock` **保持字节比较** —— 它是机读产物，差一个字节就是不同的解析结果。

### 四、实测（真机，非 fixture）

```
41 上    python3 -c '…tomllib…'  →  {"dependencies": [...]}
main 上  python3 -c '…tomllib…'  →  {"dependencies": [...]}
两侧签名相同 → 漂移门放行
```

反向对照：把 `redis>=5.2` 改成 `5.3` 后签名不同 → 仍然拦下。

### 五、守卫

`tests/test_cd_deploy_scripts.py` 新增两条，且都做过变异验证：

| 变异 | 结果 |
|---|---|
| 把语义比较改成 `if false` | `test_deploy_compares_pyproject_by_declarations_not_bytes` 转红 |
| 让签名忽略 `dependencies` | `test_dependency_drift_ignores_comment_only_changes` 转红 |

第二条的变异是关键：只钉「注释不影响签名」会让「签名恒为空」也通过，所以测试里必须有
一条真改依赖的对照。
