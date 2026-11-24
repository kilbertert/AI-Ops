# 323. 继承到旧合同候选资源时弃用重铸而非永久拒收

状态：已采纳；平高 stage2b 两轮 `ENVIRONMENT_TASK_PLAN_EXPIRED` 阻塞实测暴露的
自愈缺口修复。

## 背景

`workspace_request` 在任务自身没有 `resources` 时，继承同环境同 action 同租户
**最近一条**带 resources 的任务行——这是候选资源（operation_id/
expected_app_id/name）跨执行与跨任务复用的设计通道。`operator-prepare` 曾把
继承资源的 `binding` 与本任务 `request["binding"]` 不等判为
`ENVIRONMENT_TASK_PLAN_EXPIRED` 拒收。

该拒收只在「合同确实变了」时触发，但阻塞是永久的：被 `blocked` 的任务不会
写下 resources，于是带旧 binding 的那一行永远是「最新一条带 resources」，
同租户此后每一个 `operator-prepare` 都继承同一份旧候选、在同一检查上再被
拒。首个过期候选行形成死锁，合同变更后该租户永远无法再完成
operator-prepare——平高在 `869253ce` 改 `operator_sop.md`（binding 摘要成分）
后实测两次全链阻塞即此形状。

## 决定

继承资源与本任务合同不等时**弃用并按当前合同重铸候选**（`resources.clear()`
后落入既有 `if not resources: mint()`），与消费者分支对「已提升候选」的
clear+mint 同形。

正确性支点：该检查只在继承自既往任务的 resources 上触发——本任务自己
mint 出来的 `resources["binding"]` 在写入时恒等于 `request["binding"]`，
不等即证明是旧合同残留。在飞的合同漂移（任务入队后部署改变 binding）仍由
相邻的 `request["binding"] != current_binding()` 比对拒收，不放宽。

## 被否方案

- 继承面按 `request["binding"]` 过滤、只继承同合同资源：否决，但理由
  不是不可行——过滤后 resources 为空同样落入 mint()，功能等价。否决的
  理由是作用面：过滤改在 `workspace_request` 的通用继承查询上，会把
  knowledge-prepare/knowledge-connect/consumer-prepare 的旧合同拒收
  一并静默改为重铸，超出本次实测故障的范围；继承保持合同无关、由任务
  实现层自己判 binding 也更贴合现有分层。
- 保留未决导入先对账原 operation、确认旧候选存在后再重铸：否决。合同
  变更后旧候选本就无法再用（其内嵌 SOP/工具与现模板比对必在观察步
  被拒）；导入若未落库则「保留对账」永远零命中，任务以 reconcile 形态
  继续死锁并把同一份未决 resources 传给后继任务；若已落库，孤儿残留
  与已完成任务留下的残留同类，合同本就要求「保留已创建应用与失败
  历史」。三种结局下重铸都是最优且唯一可推进的选择。
- 让 blocked 任务也回写 resources 或加 GC 清扫旧 binding 行：否决。给
  失败任务加写路径扩大副作用面，清道夫是新机制，两者都不及就地重铸小。
- 把 `request["binding"] != current_binding()` 也放宽：否决。那才是
  PLAN_EXPIRED 的本义——部署漂移后按旧请求继续写 Dify 是不允许的。

## 后果

- 合同变更（sop/tools/模型模板/凭据文件任一变化）后首个 operator-prepare
  自动弃旧铸新，不再需要人工清库；旧候选应用在 Dify 侧残留但不绑定新
  operation，与已提升候选的处理口径一致。继承到带 `attempted` 而未确认
  `app_id` 的未决资源时同样弃用（见被否方案二）。
- consumer-prepare 共用同一代码段：批准来源版本切换改变消费者 binding 时
  同样弃旧铸新，行为一致；两侧各有替身测试固定。
- 同计划同租户同 binding 的重投仍返回原 blocked 行（确定性摘要即任务
  身份）；恢复单位本来就是新计划——blocked 子任务已把 candidate-prepare
  父任务标 blocked，旧计划无论如何都已终结。
- 测试固定：同租户第二个任务在 binding 变更后 observed 且新建应用，
  防回归。
