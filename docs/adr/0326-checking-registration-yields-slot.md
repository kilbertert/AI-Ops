# 326. checking 行按节奏让位，未知登记仍保留环境互斥

状态：已采纳；只改首次安装/登记认领循环的选取节奏，不改未知结果的安全判据。

worker 每个唤醒周期先 `installation_claim` 并立即 return：一条未知登记（远端
回报 `reconcile/unconfirmed`，落 `checking`）每轮都被选中。已派发请求的冻结
对账豁免让 `current()` 每轮都放行，stage+complete 又把 `updated_at` 刷新，
于是同一行永久占据 worker 名额。worker 是全局单循环，`worker.run` 领到它就
不再回落现场预检认领，任何排队可领的 `site-preflight`（state=queued、
lease=0、完全可领）都长时间无人处理。env95 2026-11-24 实测：一条登记
`checking` 行使独立环境的现场预检十一分钟零处理。取消也解不开——`dispatched`
冻结豁免让 `fresh=True` 不得因计划过期或取消拒绝它，行原地打转。

## 决定

`installation_claim` 的 60 秒节奏从「只限 `site-install`」扩到「冷装与登记」：
`checking` 的 `candidate-register` 行在最后一次 `updated_at` 之后 60 秒内不再
被选中。冷装早已按此节奏让位，登记沿用同一形状，因为两者的未知形态相同
（远端可能已越过首写，只能只读回读）。

让位与保护是两件事，必须同时成立：

- 让位只降重领频率，行仍是 `checking`——不落终态、不释放互斥、不重分类为
  success/blocked。
- 新的冲突安装/登记/激活仍被既有 BUSY 查询拒绝：该查询把
  `waiting/executing/checking` 连同这五个写动作全部计入互斥，`checking`
  让位不改变它的一行。

已取消且已派发的登记本就走 `observe_only=True`（`attempted` 分界在 stage
返回里），worker 因而跳过 `authorize` 写授权、只跑 `observe-registration`；
本轮以真实 `worker.run` 钉住该形状，未发现需要改动的地方。

## 替代方案

**事故超时到期即终态化（block, 释放 BUSY）**：时间流逝不证明远端零副作用。
登记可能已越过首写（临时应用导入/发布、凭据签发）只是结果未知；到期就解除
冲突保护，等于用「等够久了」冒充「已证明安全」，并让新的冲突安装覆盖那份
未知登记结果。明确排除。

**取消即 cancelled**：同理，`cancelled` 只在未派发（远端可证零副作用）时成立；
已派发取消仍只读核查，符合 ADR-0310/0314 的「未知不释放互斥」。

**按 `updated_at` 全局公平轮转**：会让 queue 顺序不再表达「先到先服务」，
且需要额外的轮转游标状态；这里要解决的只是「同一行反复占名额」，节奏让位
是最小且与既有冷装先例同形的修法。

**放宽 `current()` 的冻结对账豁免**：那是「为拿到真实结论而观察」的合同，
放宽会把「未知」判成「已过期」，直接冲突 ADR-0314/0321，排除。

## 后果

未知登记仍在 `checking` 待核对，但每 60 秒让出一名额，排队的只读预检因而
得以上桌；互斥仍在，新冲突安装/登记/激活照旧被 BUSY 拒绝。已在场的毒丸行
部署后下一周期即按节奏放行，无需改账本。

残余（本片不覆盖，见 PR「未覆盖」）：unknown 登记仍无自动出口，只能由人工
按冻结结果核对；「择优闭环」仍依赖既有 `registration_intent_absent` 意图探针，
网络不确定时继续具名待核，不以时间兜底。
