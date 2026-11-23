# 318. 发布验证以「最后复验通过时刻」为确认时刻，期限终态落可恢复而非重领

状态：已采纳；不改发行物/案例期限与取消→恢复出口（ADR-0310/0315/0316 仍生效）。

发布链上 `observed_at` 同时被当作两种东西使用：业务证据的签发时刻与「全部核对
最后通过」的确认时刻。verify 步一旦 `passed`，worker 永把 `steps.verify` 里
那份 proof 原样递给远端 activate；activate 的 300s 新鲜度闸先于
`refresh_verification` 判定，过期 proof 直接拒绝，而 refresh 即使成功其新鲜
结果也被丢弃不用。三层各自叠加成死锁：

- `environment_release_trial.complete` 先按 `.consumer-verification.json` 的
  `observed_at` 做 300s 早退；随后 `read()` 明明做了真实活复验（身份/案例/
  原 run/配置/知识全部重核），写 `.verification.json` 时却把
  `observed_at` 钉回 `original_time`——确认时刻被错记为第一次消费者完成
  时刻，复验产生的新鲜确认被自己丢弃；
- `read()` 与 operator 侧 `environment_trial.read` 只认进程内租约
  （`LocalConsumer` 300s monotonic 与 `operator_cpms.candidate_authorizations`），
  租约死亡或网关重启后没有任何「重核同一证据面」的入口，尽管全部核对项
  都可由持久锚点与活状态重建；
- 于是 activate 永远收到 receipt 相同但确认时刻过期的 proof，确定性拒绝 →
  reconcile → checking → 60s 重领同一过期 activate，env95 task_8fe0bcdc 实测
  正中此形（verify 后 ~57min 才轮到 activate，release.expires_at 亦已越过）。

## 决定

期限与确认时刻分开记录：期限（`release.expires_at`、案例 `expires_at`、
180s 交付窗、300s 数据能力租约）不变；`observed_at` 定义为**最后一次全部
核对通过的墙钟时刻**，只能由真实复验刷新，不能凭改时间戳续命。

- `release_trial.read`：进程租约缺失或 `adapter.current()` 失败时回落
  `_reconfirm`——以首过时写入 `.consumer-verification.json` 的持久锚点
  （`anchors={doc sha256, release record, configuration}`）与试用文件的
  `spec/case_sha256` 重核同一证据面：发行文档逐字、release 记录、配置指纹、
  案例摘要、消费者身份指纹与租户、准入仍关闭；外加 `_require_chat_order_visible`、
  原 run 身份/成功态、业务证据齐、现行知识授权。`deliverable()` 交付窗改判
  `run.state=="succeeded"` 的历史事实——交付 TTL 不伪装成证据存在性，
  与 operator 侧 `operator_evidence` 同语义。全部通过才回写新 `observed_at`
  与同一 anchors；任一不符按 changed 拒绝，锚点缺席按 unavailable 拒绝。
- `release_trial.complete`：删 300s 早退与 `original_time` 回写；complete
  是**复证**不是交付——即使进程租约仍活也显式走 `read(..., reconfirm=True)`
  的持久锚点重核，180s `deliverable` 交付窗只约束普通
  `publication-trial-read`，不借进复证（否则交付窗关闭而租约未满的区间里，
  已证明的 run 会被误拒；锚点仍只由通过的普通 read 签下）。只有发行物
  `expires_at` 真实越过（`ReleaseExpired`）或案例窗口真实闭合
  （`CaseExpired`，对外仍 `ENVIRONMENT_VALIDATION_CASE_EXPIRED_OR_INVALID`）
  才返回终态 `{"state":"expired","error_code":"ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"}`；
  案例 `expires_at` 超出 `now+3600` 上限属**无效输入**而非已过期，与
  spec 不符、案例形状无效、锚点缺席、身份撤权、内容篡改一律按原拒绝
  上抛，以便修正输入后重核，不落不可逆终态。operator 完成度读持久
  `agent_run_store`+QA，并在租约消亡后按同一 case token 与 runtime sha
  重新调用 `environment_trial.identity`/`target` 重核现行公司身份与发布
  目标——scope 指纹须等于原 run、生效租户须等于 spec 租户、intent 的
  `case_sha256` 须与现行案例一致；`observed_at` 取最后一次全部核对完成
  后的墙钟。
- `ops publication.activate`：receipt 按「除 `observed_at` 外逐项相等」核
  同一份证据身份；随后 `refresh_verification` 的结果被**消费**——300s
  新鲜度闸落在复验返回的确认时刻上，`state` 非 passed 或身份漂移仍拒。
- `environment_publication_worker.execute`：verify 步 passed 后，每次进入
  activate 前（不止 proof 逾 300s）先经 `publication-trial-complete` 在
  目标按发行文档重核取回新 proof 并 `record` 覆盖 verify 步，再写
  source 配置、再领 activate；verdict 为 expired（或 verify 步已记
  expired）时以 `state="expired"` 完成且先于 source 写——任务落
  blocked 不被 60s 节奏重领，原页面取消→恢复照旧不要求新鲜证据。
  发行物到期、activate 失败重领的循环因此在下一轮复核拿到确定终态。
  与 installer 侧 `activate` 消费 `refresh_verification` 合计每次激活共
  两次复核——这是刻意的两个信任/时间界面（中央 worker 判 expired 终态；
  installer 传输后、准入放开前目标再核），非 stale-only 优化：fresh
  proof 也重核，每次激活多一轮 operator 身份/目标核对的代价已接受，
  不引入重设计签票或网络缓存。
  **已越过准入的中断除外**：activate 步已起步（intent started 或已记
  result）时先以 `target_task(observe_only)` 对账目标真实 phase——目标
  已 activated 则租户准入已放开，closed-admission 复核必永拒，此时按
  目标回执完成 directory 同步（不重核 proof、不重放模型、不看发行物/
  案例期限）；仍 staged 说明从未越过准入，才走上述复核→source→activate；
  activating/restoring 等 partial 相位对账为 reconcile 交受管恢复。

## 替代方案

worker 侧每次 activate 前无条件重跑 verify()：会经 `publication-trial-start`
重放三项模型，违反「不重复模型」且徒增烧量，排除。
延长 observed_at/租约到发行物期限：`expires_at` 才是发行物寿命，拿它当
确认时刻等于给未复验的过期证据背书，排除。
activate 拒过期 proof 后由 worker 解析远端错误码区分终态：transport 层错误
形状不可信，终态分类只能在证据持有者（目标 Gateway）给出，排除。
receipt 保持逐字节相等：refresh 每次回写新确认时刻，旧 proof 必然 mismatch
再死锁；改按证据身份比对后 observed_at 漂移不再是另一份 proof，采纳。
让旧格式（无 anchors）`.consumer-verification.json` 也能复验：锚点缺失时
无法证明「复验时刻与签发时刻之间零漂移」，宁可判 unavailable 交人工恢复，
排除。

## 后果

- 正路径：verify→activate 间隔任意拉长（在发行物/案例期限内）不再死锁；
  每次 activate 前由目标 Gateway 实际重核，确认时刻如实。
- 复发面：旧格式无 anchors 回执、案例/发行物过期、身份撤权、配置/目录
  漂移，各自以 unavailable/changed/identity/expired 拒绝；expired 落
  blocked 给操作者明确恢复出口而非无限重领。
- 检查面：`publication-trial-complete` 新增 expired verdict 形状；activate
  消费 refresh 结果；`.consumer-verification.json` 增加 `anchors` 字段
  （私有账本内字段，不进公共投影）。
- 恢复不变：取消→恢复链不读 verification，ADR-0310/0311 判据照旧。
