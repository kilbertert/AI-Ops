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
- `release_trial.complete`：删 300s 早退与 `original_time` 回写；`document/
  require_spec/load_case` 的拒绝归入终态，返回
  `{"state":"expired","error_code":"ENVIRONMENT_PUBLICATION_VERIFICATION_EXPIRED"}`；
  operator 完成度改读持久 `agent_run_store`+QA 状态，不再要求进程租约。
- `ops publication.activate`：receipt 按「除 `observed_at` 外逐项相等」核
  同一份证据身份；随后 `refresh_verification` 的结果被**消费**——300s
  新鲜度闸落在复验返回的确认时刻上，`state` 非 passed 或身份漂移仍拒。
- `environment_publication_worker.execute`：`steps.verify.state` 为
  passed 而 `observed_at` 逾 300s 时，先在目标经 `publication-trial-complete`
  重核取回新 proof 并 `record` 覆盖 verify 步，再领 activate；verdict 为
  expired（或 verify 步已记 expired）时以 `state="expired"` 完成——任务落
  blocked 不被 60s 节奏重领，原页面取消→恢复照旧不要求新鲜证据。

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
