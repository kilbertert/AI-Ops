# 45. CI 在「有人会用到结论」的那一刻跑

日期：2026-10-19
状态：Accepted
影响版本：`.github/workflows/ci.yml`、`afk-policy.yml`、`spec-tree.yml`（各加 job 级 draft 守卫）；
        `.github/workflows/agent-implement-pr.yml`（改之前标回 draft）

## 背景

一个 AFK 的 PR 会把这套检查跑很多遍。实测 #825：本仓三个 workflow 各跑 **11 次**
（8 success + 3 cancelled），约 16 分钟 runner 时间；#824 各 7 次。

先问「CI 到底在回答什么问题」，答案决定了哪些运行是有价值的：

**CI 唯一回答的是「这份内容能不能进 main」。** 它不回答「agent 写到一半的东西对不对」——
那个问题由 agent 本地回答：`.sandcastle/implement.md` 第 3 条要求它提交前跑本仓自己的检查。

由此得到判据：

> 一次 CI 运行值得它的成本，当且仅当
> **(a)** 它覆盖的是即将进 main 的内容，**且 (b)** 那一刻的结论会被用到。

draft 期间的运行两条都不满足。

**而且确实没有任何东西用它**（这是判据 (b) 的取证，不是推断）：

```
grep -rn 'gh pr checks|statusCheckRollup|/check-runs' .sandcastle/ .github/
→ 空
```

没有任何 agent、评审、workflow 读 PR 的 CI 结论。

## 决策

**draft 期间不跑；`ready_for_review` 之后照跑。** 实现成 job 级守卫而不是
`types: [...]` 过滤器：

```yaml
if: >-
  github.event_name != 'pull_request' ||
  github.event.action == 'ready_for_review' ||
  github.event.pull_request.draft == false
```

## 被实测推翻的两个假设（都发生在写代码之前或之中）

**1. 「GitHub 原生跳过 draft 的 pull_request workflow」——不成立。**
draft PR #823 只有 3 个 run，看起来支持这个假设；但查下去发现它**总共只 push 过 1 次**，
3 个 run 就是那一次的三个 workflow。而 #825 **从未**标过 ready，却跑了 11 次。
若照这个假设去设计，会写出一条**什么都不省**的改动。

**2. 「只测 `draft == false`」——会把唯一该跑的那一刻也跳过。**
实测 PR #833：draft 时三个检查都是 `skipping`（正确）；**标 ready 之后没有任何重跑**，
PR 停在「三个检查全是 skipping、且没有途径重判」的状态。
原因是 `ready_for_review` 事件的 `pull_request.draft` **仍读作 `true`** ——
payload 描述的是这次转变，不是转变之后的状态。
所以 `ready_for_review` 必须显式放行；这不是细节，是让守卫**正确**而不是仅仅**安静**的东西。

## 审查抓出的洞：`skipped` 满足必需检查

这是本记录最重的一条，也是最反直觉的一条。

**job 级 `if` 被跳过时，检查结论是 `skipped` —— 而 `skipped` 是满足必需状态检查的。**
三条 workflow 的 `pull_request` 触发原本用 GitHub 的默认 activity types
（`opened, synchronize, reopened`），**不含 `ready_for_review`**。于是：

```
draft 打开   → 三个 job 全 skip → 检查记作 skipped（满足必需检查）
标 ready     → 不在订阅列表 → 不起任何新 run
不 push      → 这份内容可以未经检查进入 main
```

我的第一次验证之所以看起来通过，是因为**标 ready 之后我还 push 了一次** ——
把 push 的效果记到了 ready 头上。把两者分开才看得见洞。

修正：三个 workflow 的 `pull_request` **显式列出** activity types：

```yaml
pull_request:
  types: [opened, synchronize, reopened, ready_for_review]
```

两层各管一件事，缺一不可：

| 层 | 作用 |
|---|---|
| `types: [... ready_for_review]` | 标 ready 时**起一次 run** |
| job 级 `draft == false` 守卫 | draft 编辑期间**不起 run** |

只有守卫 = 标 ready 后仍未验证；只有 types = draft 每次 push 都跑。**两个一起才是设计。**

## 被否掉的替代形态

- **`types: [ready_for_review]`（只在该事件跑）。** 看起来最贴合本决策的意图，
  也最短。它输在**会让非 draft 的 PR 永远不跑** —— 手工开的 PR 在 `opened` 那一刻
  就已经是 ready 的，而 `opened` 不在列表里。本仓的 PR 不全是 AFK 建的，
  这条会把「开了 PR 却没有任何检查」变成默认行为。

- **`paths:` 过滤（按改动路径决定跑不跑）。** 直觉上能省更多，也是我在上一轮
  就已经否过的同一形状：`tests/test_readme_architecture.py` **断言 `README.md` 的内容**，
  所以按路径跳过纯文档改动会让改文档绕过读它的测试。而且本仓最近 40 个提交里
  纯文档只占 2 个 —— 省不到 5%，却引入一个静默洞。

- **依赖平台跳过 draft（什么都不写）。** 这是最省事的形态，我也确实先假设它成立。
  实测否掉：#825 **从未**标过 ready 却跑了 11 次 —— 平台不替我们省。

- **把 `draft` 判断写在 workflow 内侧的 step 级 `if`（而不是 job 级）。**
  区别是 job 级跳过会让整个 job 显示 `skipping`（一次运行都不起），
  step 级仍会起 runner、装依赖、再逐个跳过 —— 省不到节点时间，只省了测试时间。

- **什么都不做，接受这 16 分钟。** 这是个正当的选项，我把它写在最前面因为它最容易被
  跳过：如果 CI 是免费的、或者结论有人读，那么 11 次里每一次都有价值。
  它输在两点都可证伪：结论**没人读**（全仓 grep 为空），而每个 PR 约 16 分钟的
  runner 时间在共享的 self-hosted runner 上是排队的。

## 后果

- **实测**（同一个 PR #833）：draft → 三个检查 `skipping`；标 ready 后 push →
  `verify` pass、`spec-tree lint` pass、`Workflow policy` 跑到了它该跑的检查。
- **代价（说清楚）**：draft 期间 PR 上没有任何 CI 信号。这是有意的 ——
  它把「这份内容对不对」的举证责任放回 agent 本地，而那里本来就有这道检查。
  一个**在 draft 期间被合入的坏改动**不会被 CI 拦，但 draft 本来就不能合。
- **仍未做**：`agent-implement-pr.yml` 在已 ready 的 PR 上提交修改时仍会触发全套 ——
  它需要在动手前把 PR 标回 draft、改完再 ready。这是本决策的另一半，
  不改它这个 PR 只解决「创建时就 draft」的那条路径。
