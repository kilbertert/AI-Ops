## 2026-10-23 · CHANGELOG 拆成「生成物 + 发布说明」

> **未完成业务验收。** 这一篇记的是文件职责与门禁：判据是生成结果的实测比对与
> 本地可复现的检查。它**不**包含工程师确认的真实故障案例，也**不**是用户侧业务验收。

### 一、上一轮的判断被验证推翻

ADR 0098 把 `CHANGELOG.md` 记为「能从 commit 重建却仍手改」。动手前先比对：

```
生成版（git-cliff）：435 行 / 416 条纯 commit 标题
仓库版            ：580 行 / 524 条含解释与 ADR 引用的人工条目
交集              ：0 行
```

**零交集**。它不是「被误提交的生成物」，是两件不同的事被塞进了一个文件名。

### 二、处置

| 文件 | 角色 | 写入者 |
|---|---|---|
| `CHANGELOG.md` | 生成物 | 只有 `dev-changelog .` |
| `RELEASE-NOTES.md` | 人工发布说明（由原 `CHANGELOG.md` 改名） | 里程碑作者 |

### 三、判据（可复现）

```bash
# 手改必然失败；重新生成必然通过
dev-changelog --check .
```

实测：

```
手改一行 → exit 1（does not match the commits it is generated from）
重新生成 → exit 0
```

门禁放在 `dev-policy-audit`（每日、开发机），**不是 CI**：`AFK Policy` 跑在
`ubuntu-latest`，上面没有 `dev-changelog`（随 release unit 安装）也没有 `git-cliff`。
在 CI 里装一套，断言的就是第二个生成器。CI 里保留说明，不留假装在检查的步骤。

### 四、边界

- 警示头写在共享策略工具的模板里，重生成会被覆盖。所以「不要手改」靠门禁，不靠文案；
  改模板属于 release unit 的变更，不在本片范围。
- `CHANGELOG.md` 仍留在版本控制里（与 `REPO-MAP.md` 不同）：它是给消费方读的产物，
  读者不该先跑工具。它能留的前提是**只有生成器写它**，这一条由清单持续核对。
