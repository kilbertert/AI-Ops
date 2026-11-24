# 325. 容量并发证明按窗口起点发令，而非同刻抛三腿

状态：已采纳；修正 `environment_capacity.run` 的三腿发令顺序，不改重叠判据、采样闸与余量阈值。

容量的并发证明取三段**实际 run 窗口**（`environment-registry.md` D43：三项 run
没有实际重叠也拒绝，不能以发起三个线程冒充并发容量完成）。但 consumer 腿在
它自己的 run 落库之前有一段十余秒的串行准备：95 侧候选租约（Redis third_session
解析、installed 校验、CPMS 可见性），41 侧 `RemoteCandidate` 快照/Dify console
读/export_dsl/claim，之后才 `start_trial` 创建 `chat_dify_runs` 行。三个
`business_run` 同时投入线程池时，这段准备不被计入窗口，短腿却已经在跑。

env95 2026-11-24 实测：order `claimed` 06:36:05.209→`completed` 06:37:00.812
（95 `agent_runs`），offline 06:36:05.328→06:36:17.601，consumer 06:36:19.595→
06:37:25.430（41 `chat_dify_runs`）。`max(started)=06:36:19.595` 比
`min(completed)=06:36:17.601` 晚 2.0s → `concurrent business capacity proof
incomplete`。三腿各自真实 passed，失败只在窗口错开；offline 的真实 run 只有
12.3s，比 consumer 的预热还短，因此同刻发令时必然跑在 consumer 起跑之前。
xiaoqu 历史里同一形状还出现过一次（order 10.6s 在 consumer run 创建前 8s 结束），
也侥幸过一次——按同刻发令，这条闸是掷硬币。

修复只改发令顺序：consumer 先起跑，`consumer-start` 返回（run 已落库）后置位
一个事件，order/offline 两腿在各自 `business-step` 之前等待该事件（上限 150s，
consumer 腿失败时由采样循环放行，让各自的失败照常经 futures 收敛）。窗口判据
仍是 `max(started) < min(completed)`，采样频率、80% 占用上限、1GiB 宿余量、
900s 容量新鲜度闸一律不变——三 run 依然必须真实重叠，只是不让「慢预热」冒充
「不同时」。

## 替代方案

- **放宽重叠判据**（例如允许 5s 容差、或要求两两重叠即可）：直接削弱这条闸
  要证明的东西，而 window 错开恰恰是「没有同时承载」的证据，排除。
- **把 consumer 预热移进被测窗口**（以 `business-step` 开始时刻当窗口起点）：
  预热不承载被测业务，把它算进并发会高估同时承载量，且 95/41 两侧时钟与记录
  位置不同，排除。
- **重试直到撞上重叠**：成功与否取决于两段随机时长，是不可复现的验收，
  排除。
- **给 `business-step` 增加「准备完成」子动作**以协议化预热：新增一次跨端往返
  与半成品标记面，且 `consumer.json` 的半成品语义已是 ADR-0316 的续跑边界，
  扩大该面收益不抵复杂度，排除。

## 后果

- 容量链在慢预热腿真实起跑后才让短腿发令，重叠证明变成「确定性成立」而不是
  「碰运气成立」；总时长上限增加一次 `consumer-start` 的等待（实测 ~15s），
  远小于 300s 任务租约与 900s 计划窗。
- consumer 腿起跑即失败时不再空等 150s：采样循环在其 future 落定后立即放行，
  异常照原路经 `f.result()` 抛出，worker 的既有收敛（reconcile/核查）不变。
- 未改窗口起止取数：operator 腿仍用 `claimed_at`，consumer 腿仍用
  `created_at`——两者都是各自 run「开始承载」的时刻，语义一致。
- 续跑边界不在本修订面：`consumer.json` 半成品被硬拒（`START_UNCONFIRMED`）仍
  按 ADR-0316 的出口处理（取消→恢复→新发布任务）。
