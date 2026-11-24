# 326. 两条认领面按唤醒严格交替，长期不还名额的一侧不得饿死另一侧

状态：已采纳；只改 `environment-task-worker` 每次唤醒先领哪条认领面（及其轮转游标），
不改任何安全判据、不改 BUSY 互斥、不改任务终态规则。

worker 每次唤醒先领安装面：`installation_claim` 一旦 `claimed` 就立即 return，站点
认领（`{"owner": ...}`）永远执行不到。一条未知登记（远端回报
`reconcile/unconfirmed`，落 `checking`）已翻转 `dispatched`，`current()` 因而跳过
计划窗与取消闸、永不自行终态——每次唤醒都可再领，`stage+complete` 又刷新
`updated_at`。于是只要**任意一条** checking 可领，worker 就走安装分支并 return，
排队的 `site-preflight`（state=queued、lease=0、完全可领）永远轮不到。

这不止是「一条毒丸」：登记 checking 是设计内的长期形态，跨环境可以同时存在多条。
用 `origin/main` 原始代码实测：

- 单条 checking + 一条 queued 预检：预检十一分钟零处理；
- env a、env b 各一条 checking + env b 一条 queued 预检：唤醒序列永远是
  `checking_a, checking_b, checking_a, checking_b, …`，预检一次都不被处理。

## 决定

worker 每次唤醒由中央账本上的一个持久轮转游标决定**本次先领哪条认领面**，两侧
严格交替（`site` ⇄ `installation`）；先领的一侧无可领时才退回另一侧。游标在既有
每次唤醒必调的 `advance_candidates` 调用里翻转并随响应返回（`{"turn": "site"|
"installation"}`），worker 据此排序，不新增端点、不改既有字段。

为什么是「交替」而非任何一种定序：

- 两侧都可能**长期不还名额**。登记 checking 已派发即跳过计划窗/取消，永不终态；
  现场预检的 reconcile 行同样跳过计划窗（`_current(reconciling=True)`），
  一条远端持续待核的预检也不会自行排空。任一固定优先序，只要对方「够多」就会把这一
  侧永久遮住——`site` 优先在「≥3 条长期 reconcile 的现场行」时饿死安装面，
  `installation` 优先在「≥1 条长期 checking 的登记」时饿死现场面，二者是对称的同一
  缺陷（均已实测复现）。
- 严格交替把「某一侧每 N 轮最多占 N-1 轮」变成不变量，与两侧各有多少条无关：
  每一侧至多被跳过一轮。

游标持久在中央账本（`environment_task_turn` 单行表，建在既有 `initialize_tasks`
里；旧账本就地补建），进程重启、多 worker 并发都不会重置或丢步；缺行以 `site`
起手。

以下边界不变：
- `checking` 行仍是 `checking`——不落终态、不释放互斥、不重分类为 success/blocked；
- 新的冲突安装/登记/激活仍被既有 BUSY 查询拒绝（查询本身一行未改）；
- 安全判据（计划窗、副作用证明、租约、冻结对账）一律不动。

已取消且已派发的登记本就走 `observe_only=True`（`attempted` 分界在 stage 返回里），
worker 因而跳过 `authorize` 写授权、只跑 `observe-registration`。本轮以真实
`worker.installation_run` 钉住该形状：替身真实走 `stage`，断言 `observe_only=True`
时绝不发 `authorize`、`False` 时必发。

## 替代方案

**单条 60 秒冷却让位（把 `candidate-register` 纳入既有的 `site-install` checking
节奏）**：只在同一条反复被选中时有效。跨环境多条 checking 交错时，任意一条冷却期外
即可补位（实测 N≥3 时每轮都有可领的一条），排队预检仍被饿死——只是把饿死延后，不是
保证。单毒丸测试会绿而多毒丸测试仍红，正是被否的理由。

**「现场预检先领」定序**：对 installation→site 方向的饥饿有效，但对相反方向不成立
——现场面自身长期 reconcile（远端持续待核、跳过计划窗）时会对称地饿死安装登记
（实测：3 条长期 reconcile 的现场行使安装登记零处理）。用定序解决对称缺陷只能治
一半，排除。

**按 `updated_at` 全局公平轮转**：会让 queue 顺序不再表达「先到先服务」，且要改
两张认领表各自的选取 SQL；而缺陷只在「两条面之间谁先手」，在面这一层交替更小、
更直接。

**事故超时到期即终态化（blocked，释放 BUSY）**：时间流逝不证明远端零副作用。登记
可能已越过首写（临时应用导入/发布、凭据签发）只是结果未知；到期解除冲突保护等于用
「等够久了」冒充「已证明安全」，并让新的冲突安装覆盖那份未知结果，排除。

**取消即 cancelled**：`cancelled` 只在未派发（远端可证零副作用）时成立；已派发取消
仍只读核查，符合 ADR-0310/0314 的「未知不释放互斥」。

**放宽 `current()` 的冻结对账豁免**：那是「为拿到真实结论而观察」的合同，放宽会把
「未知」判成「已过期」，直接冲突 ADR-0314/0321，排除。

## 后果

一条或多条跨环境 checking、以及任意多的长期 reconcile 现场行，都不再能饿死另一侧：
每一侧至多被跳过一轮。未知登记仍是 `checking` 待核对（本片不解决它的自动出口），
互斥仍在，冲突安装/登记/激活照旧被 BUSY 拒绝。已在场的毒丸行部署后无需改账本，随
下一次唤醒即按交替顺序让位。

代价：任一侧繁忙时另一侧至多每两轮被服务一次（交替的固有下界，不是退化）。安装面与
现场面都由 `updated_at`/租约驱动、下一次唤醒继续，无正确性影响。

残余（本片不覆盖，见 PR「未覆盖」）：unknown 登记仍无自动出口，只能由人工按冻结
结果核对；「择优闭环」仍依赖既有 `registration_intent_absent` 意图探针，网络不确定
时继续具名待核，不以时间兜底。
