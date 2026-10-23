## 2026-10-23 · 冲突面完整清单：105 个分支逐个枚举，未决热点归零

> **未完成业务验收。** 这一篇记的是工程侧的冲突来源与可执行判据：数据来自
> `git merge-tree` 对真实远程分支的枚举。它**不**包含工程师确认的真实故障案例，
> 也**不**是用户侧业务验收。

### 一、上一轮漏了什么

ADR 0097 拆了两个证据日志后，报告里写了「冲突面归零」。对 105 个远程分支完整枚举
（`.sandcastle/conflict-surface.mjs`）后，真实的冲突分布是：

```
65  docs/validation.md                    ← 已由 0097 解决
60  docs/开发进度.md                      ← 已由 0097 解决
34  .sandcastle/REPO-MAP.md               ← 生成物，漏掉了
15  .afk-bootstrap.json                   ← 生成物，漏掉了
12  docs/agents/current-delivery-state.md ← 手写索引，漏掉了
 8  deploy/m2-live-03/images.lock.json    ← 生成物，漏掉了
 8  README.md
 7  acceptance.feature
 ...
```

**上一轮也没有意识到邻近形态的存在** —— `docs/agents/pr-conflict-stops-ci-dispatch.md`
（2026-10-23 早先记录）已经把「PR 冲突时 CI 不派发」的症状与判别顺序写透了，缺的正是
「消除来源」这一半。

### 二、判据（可复现，每次现算）

```bash
# 全量分类报告：每个冲突路径 + 处置，末尾给出 hotspot 数
node .sandcastle/conflict-surface.mjs

# 表格自检：同一路径被分类两次、或有处置没写理由，一秒内失败
node .sandcastle/conflict-surface.mjs --self-check

# 门禁：已移出版本控制的路径不得再被跟踪
node .sandcastle/conflict-surface.mjs --check
```

**通过判据：`prose hotspots at 3+ writers` = 0。**

当前输出：

```
1 removed from version control · 2 single-writer by design ·
29 shared below the threshold · 6 serial by design
60 source modules with concurrent edits (rebase, never split) ·
0 prose hotspots at 3+ writers
```

### 三、处置与依据

| 处置 | 路径 | 依据 | 证据 |
|---|---|---|---|
| `removed` | `.sandcastle/REPO-MAP.md` | 被跟踪文件集的纯函数 | 加一个文件只改 2 行（`173→174`、`20 more→21 more`），零信息量 |
| `exclusive` | `.afk-bootstrap.json` | 一个仓库一个模板版本，升版本天然串行 | `upgrade-afk.sh` 是唯一写者 |
| `exclusive` | `deploy/m2-live-03/images.lock.json` | 一次构建一个身份，只有 `--update-lock` 写 | `build-image.sh:28` 注释明说「构建与记录是两件事」 |
| `serial` | `acceptance.feature`、`qa-plan.md` | 端到端场景套件，横跨整个文件扩展 | 7 天 17–18 次提交，非单端堆叠 |
| `serial` | `docs/agents/env-41-runbook.md` | 运维手册要按顺序读 | 尾部追加（`@@ -2030,0 +2031,111 @@`） |
| `serial` | 5 个 `.sandcastle/implement*` prompt | 唯一内容来源是脚手架升级 | `git log` 显示全部由 `chore(afk)` 提交 |
| `serial` | `docs/adr/0033-...` | 「落地情况」小节插在同一位置 | `@@ -86 +87`、`@@ -97 +98`、`@@ -108 +109` |
| `source` | 60 个源码模块 | 并发不是格式缺陷 | 绝大多数只有 1–2 个分支 |

### 四、REPO-MAP 移出版本控制的验证

```
$ node .sandcastle/repo-map.check.mjs
repo-map check ok (162 lines, graph project AI-Ops, deterministic)
```

新增的**确定性断言**：同一棵树两次渲染必须逐字节相同。这是取代「提交副本比对」的
性质 —— 地图改为读取时产生后，若生成器每次给出不同结果，冲突只是从 git 移到了读者。

### 五、诚实的边界

**第三类冲突不会消失。** 60 个源码模块 + 6 个 `serial` 散文文件仍然会在并行分支上
冲突，因为它们的写者确实改同一处。这部分靠调度控制（同一模块的重写一次一人），
不靠文件布局。任何声称「冲突已彻底解决」而不区分这两类的说法，都是本文开头纠正过的
那个错误。

**一个已知未决项被留在清单里**：`CHANGELOG.md` 可从 Conventional Commits 完整重建，
却仍被手改入库 —— 下一个该 `removed` 的候选。写进表里，不悄悄略过。
