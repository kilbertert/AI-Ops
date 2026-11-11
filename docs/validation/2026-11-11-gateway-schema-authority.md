# Gateway schema 单一权威：作者验收

状态：作者验证、独立Hack复验与最终一致副本检查完成；1.7.0尚未上线，未完成业务验收。
任务 #1462，Refs #1448；实现基线e60a6049。#1461已随1.6.1上线，见阶段1交付记录。
本轮获准提交和创建PR，合并与部署另由交付会话执行。#1448不随本PR关闭，#1463历史结构归一延期。

## 已确认判据与本轮记录时序

判据来自实施前已确认的#1448分阶段计划与初始任务说明，
不是从候选输出反推。本文件的初始 Gherkin 在本会话 DDL 搬移前写入；
下方 QA 对照与并发 WAL 反例在实现和父反馈后补写，不宣称它们也提前落盘。

来源：用户确认的分阶段规格；固定历史提交 9080a463（Dify）与 44260454（Agent）；
SQLite 官方事务、PRAGMA、ALTER TABLE、WAL 与 Python sqlite3 backup 文档。
新增长期检查拦截已复现的构造写锁与半迁移，以及新版本治理的数据损失边界；
原有单 store 测试不能证明全库原子升级、跨进程锁内重读和同版本漂移拒绝。

完整判据见 [Gherkin](../acceptance/gateway-schema/acceptance.feature) 与
[QA 计划](../acceptance/gateway-schema/qa-plan.md)。

## 作者证据（2026-11-11）

本地目录：`dist/verification/gateway-schema-authority/`；完整索引与候选摘要见其中的`HANDOFF.md`。
本文只发布脱敏检查结果和可复验命令；原始内部报告、业务源、私有副本及其位置不公开。

| 检查 | 结果 | 证据 / 版本范围 |
|---|---|---|
| 干净全量 | 4880通过、9跳过，255.58秒 | `full-clean.txt/xml`；节点列名单补丁前 |
| 最终合同定向 | 60通过，29.36秒 | `final-contracts.txt`；含列名单补丁、真实peek/app、旧凭据重复清理 |
| schema+目录/版本/文档 | 72通过 | `final-focused.txt`；列名单补丁前，schema声明未再改 |
| Ruff | check与format通过 | `ruff-check.txt`、`ruff-format.txt` |
| 风险覆盖率 | schema93%、连接94% | `coverage-tests.txt/json`；83通过，非全仓覆盖目标 |
| 受控变异 | 提前version、删锁内重读、漏补列均被捕获 | `mutations.json`和三份红灯日志；临时拷贝，不改工作树 |
| SQLite3.45.1 WAL | 父40轮18错误→修复后40轮0错误 | `wal-repro-fixed.txt`；本地兼容环境，非生产在线 |
| 部署脚本 | 真实片段备份/迁移/检查失败拒绝，旧库不被覆盖 | `deploy-tests.txt`；随后完整部署套件进入绿灯全量 |
| Gateway临时进程 | health200、匿名401、OpenAPI200、退役404 | `gateway/aiops-gateway.json`；最后候选再次通过 |
| wheel/sdist | 1.7.0构建成功 | `packages/`、`build.txt` |
| Gateway镜像 | 构建、容器导入与反序节点表写入通过 | `gateway-image/identity.json`、`container-import.txt`；容器SQLite3.46.1 |
| 声明搬移对照 | 28表/21索引语义相同 | `declaration-movement.txt`；对照改前初始化快照 |
| 导入副作用 | schema/DifyWorkspaces导入无数据库I/O | `import-no-io.txt` |
| ADR | 四个提交的逐提交门禁通过 | `committed-adr.txt`；基于最新origin/main |
| worktree | audit通过 | `worktree-audit.txt`；不清理用户工作树 |

9项skip：1项空串由调用方短路，8项缺少公司GitLab身份锚点；没有把skip算通过。
本轮已有业务行为套件绿灯，但真实Dify/Java、KB活性、故障诊断准确率均未验。

## 提交与主线同步

候选按职责形成四个提交：静态schema与连接原语、store接线和旧DDL移出、启动/运维/部署及行为检查、
文档/版本。随后无冲突接入#1465文档合并后的默认分支；实现文件摘要与独立复验一致。
提交后文档/目录/版本与真实HEAD部署dry-run共39项检查通过；Ruff与wheel/sdist通过。
部署dry-run使用仓库dev-host替身，候选制品版本为1.7.0加提交标识，不连接生产。
未重复本地全量；最终提交的全量确定性CI由PR执行，不能用补丁前4880/9代替。

## 失败与修复

- 首轮全量4833通过、9跳过、8失败：WAL并发是真缺陷；其余为旧夹具缺少Gateway身份、
  同版本删列后仍期待自动修复、裸INSERT两列、部署测试取段边界及VERSION取自工作树。
  逐项修正历史夹具和候选revision取值；没有放宽未知结构或业务断言。
- 40轮双线程WAL竞争在SQLite3.45.1稳定复现。仅模式切换的SQLITE_BUSY有界重试5秒，
  非BUSY直接抛出；业务写和任意迁移异常不重试。
- 第二轮全量4878通过、9跳过，唯一目录失败是作者coverage子进程生成根层临时文件；
  文件移入证据目录后38项目录/文档/版本通过，再跑干净全量得到4880通过。
- 作者补上`sqlitex_business_data`反例：`LIKE sqlite_%`误把下划线当通配符，改GLOB精确前缀。
- 独立Hacker经父转述发现节点记录按物理列序插入：固定反序DDL启动通过，原record查不到行。
  最小修复为显式列名单；`node-column-order-red.txt`记录0.23秒红灯，
  `node-column-order-green.txt`记录44通过。AST全仓扫描原仅此一处，修复后0处。
  后续最终合同60通过；按父要求不重复全量，交付CI需验证最终提交。
- 一次临时对照误用未隔离的Python环境，未得到对照结论；显式选择候选环境后28表对照通过。

## 独立行为Hack与缺陷闭环

独立Codex会话，2026-11-11 03:46–03:59 UTC完成实测；精确模型标识未提供，不猜测。
先读确认计划、ADR与QA，再用固定历史DDL和自建合成行独立驱动真实入口。
共发现1项节点列序缺陷，作者显式列名单修复后，原反例与新的四列反序输入均复验通过；
同一schema源码摘要为`1feeac95ccad8c96be23e6a513330f5f40635a09e94e06e31d87b2dd87ff14d0`。
已执行范围没有未闭合的实测缺陷，不能由此推导全部业务或线上迁移通过。

| 范围 | 独立实际结果 |
|---|---|
| SQLite3.45.1核心 | 四进程空/历史库初始化及快检后屏障，均仅一次最后版本写入 |
| 原子性 | 四阶段execute写后异常、补列后进程退出、真实COMMIT失败，结构/行/版本回滚且可重试 |
| WAL | 真实BUSY约5秒退出、释放后成功；非BUSY错误仅尝试一次并关闭连接 |
| 稳态 | 15类构造、14类实际纯读在外部写锁下0.32–3.66ms；trace无BEGIN/DDL/DML；会话只验构造 |
| 漂移 | 列、默认、PK、FK、unique、索引、collation、partial、CHECK、冲突策略等11类均拒绝 |
| CLI与凭据 | 缺文件不创建，错误库/未来版本拒绝且输出无业务值；写锁下check可读；旧字典两轮清理保持secure_delete |
| Route/TTL/Mapping | 独立跨表发布、旧revision拒绝、异常回滚；peek不更新、get收敛过期；禁GC重复读取无fd增长，Mapping独立 |
| 部署与恢复 | 一致备份含WAL行；成功/已有备份/漂移/补列异常/完整性失败/权限失败六路径符合边界；代码恢复保留数据库新写入 |
| 旧程序/真Gateway | 已发布程序读取升级合成库保持身份和值；临时Gateway启动、匿名401、规范200、退役404通过 |
| 变异 | 提前版本、删除锁内重读、遗漏补列三个变异全部被独立判据捕获 |

CLI综合轮使用SQLite3.51.2，另有SQLite3.45.1写锁下check；核心及store兼容轮明确使用3.45.1，
不同环境不混算。独立轮未覆盖完整真实Dify/Java/KB链路、所有业务方法、跨HTTP取消/撤权压力，
带旧凭据的完整进程重启链也未单独执行，未完成业务验收。

## 一致副本证据的时点

- 早期（约03:30–03:34 UTC）：两种SQLite环境副本升级保留33表逻辑值，4类构造在外部写锁下快速返回。
  这是提前反馈，不冒充最终候选。
- 最终（03:55:05 UTC）：交付会话在新一致副本上以SQLite3.45.1执行startup/integrity/check，
  schema摘要与上文一致，版本1，33表（28受管+5额外）的逻辑行数及按列排序的值摘要全部不变；
  integrity为ok，FK错误0。节点列名单修复不改变schema定义。
- 旧发布程序兼容：在另一升级副本上构造Dify/Agent/SelfService/Route/KeyOperations，
  33表原逻辑值不变、版本1、quick_check通过；此轮使用依赖齐全的Python3.13.13环境，
  不声称该真实副本旧程序回放也在SQLite3.45.1完成。

副本检查不是生产在线迁移；本文不复制原始报告、私有位置或业务值。

## QA 对照（收到父反馈后补写）

| ID | 用户入口 / 输入 | 必须观察 / 拒绝 | 当前证据 |
|---|---|---|---|
| DB-01 | 当前版每类构造 + 纯读，外部进程持写锁 | <1s；trace 无 BEGIN/DDL | 最终合同定向通过 |
| DB-02 | 固定历史行，四阶段写后异常 | schema/rows/version 全回滚 | 最终合同定向通过 |
| DB-03 | 两进程空/旧库；40轮双线程空库 | 全部成功、锁内重读；BUSY总等待有界 | 父反例18/40失败；修复后0/40，长期并发检查通过 |
| DB-04 | future/unknown，同版缺列/索引/默认/约束漂移 | 拒绝、不修逻辑库 | 最终合同定向通过 |
| DB-05/06 | 9080a463 Dify、44260454 Agent 固定 DDL | 行值/身份保持；仅指定 scope_json 默认差异 | 最终合同定向通过 |
| DB-07 | 独立 route publish(source_agent_name) / CAS / 异常 | registry 与 route 同事务、失败无部分提交 | 最终合同定向通过 |
| DB-08/09 | Mapping/原 LEASE/TTL/权限回归与真实临时 Gateway | 不破坏原合同 | 全量4880通过及Gateway日志 |
| DB-10 | 一致 backup / 副本 migrate-check / 旧程序 | 不覆盖源库，保留 WAL 行；旧程序兼容 | 独立合成库回放与交付会话一致副本摘要通过，见上文环境区分 |
