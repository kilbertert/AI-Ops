# 328. 已取消的未知登记凭远端资产证据终态化

状态：已采纳；只在「已取消」且「远端读得出资产事实」时新增一条终态出口，
其余 checking/互斥/计划窗/授权规则一律不动。

已派发且被取消的 candidate-register 任务落 `checking`（远端回报
`reconcile/unconfirmed`），设计内只有两条终态出口：远端报
`observed`（registered/unchanged），或 `absent` + 意图探针证缺席。而当
远端观察本身拿不到 verdict——例如部署代码的 `SETUP_ROUTE_CONFLICT`
把本血统发布留行误判为占用（ADR-0327）——两个出口都不可达，任务每周期
被领取、永不终态，BUSY 把该环境全部 installation 面提交永久锁死
（env95 task_d7460034 实证）。

已取消任务对「副作用是否未知」其实可由两个只读事实收束：

- 登记意图文件在场（既有 `registration_intent_absent` 探针，不可核验
  收敛到在场）＝该任务已越过 register() 的首个落盘写；
- 登记资产（工作区 + 双端应用行）在场/缺席——register_runtime 在同一
  事务里写它们，原子落盘：**在场即原登记已提交**，**缺席即除意图文件外
  零写**。

两个方向对已取消任务都给出可证有界的副作用：资产在场说明登记目的早已
达成（随后取消只是账本动作）；资产缺席说明提交未发生。两种情况判
`cancelled` 都诚实，且把证据写进结果存档。

## 决定

- worker 在远端登记回报失败的既有兜底里追加一次
  `registration_assets_present` 只读探针（与 `registration_intent_absent`
  同契约：dev-host exec、环境锁内只读、退出码协议 0=在场/7=缺席/其余未知），
  探针答出时在 `unconfirmed` 结果上附 `remote_assets: present|absent`；
  答不出则不带字段——未知不假装成缺席。正常回报 `absent` 而意图缺席
  不可证的路径同样先核资产再回报。
- 中央 `complete` 形状闸放行 `remote_assets`（仅允许与
  `absent`/`unconfirmed` 同现、取值封闭）；终态规则新增一条：`cancel=1`
  且 `remote_assets` 已知 → `cancelled`。未取消任务不因此放行——资产
  在场只证「原登记已提交」，不证本冻结目标登记的 verdict，互斥继续保持。

被否方案：给 checking 加时间窗自动终态化——时间流逝不证明远端零副作用
（COORDINATION-REGISTRATION 明确拒绝）；转发远端 stderr 判失败类型——
对「本次调用零写」的论证不如直接读资产状态牢靠，且 stderr 形状跨层易碎。

worker 侧实测前提（env95 回放）：远端 `observe-registration` 必走
`register_runtime`，其冲突谓词命中发布血统行必抛错；探针直读
`/var/lib/aiops-access-95/gateway/gateway.db` 同一活库求值，行时间戳
早于任务创建可证非本任务副作用。
