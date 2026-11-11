# 数据库初始化第一阶段：事务、纯读租约与连接关闭

目标：通过#1461执行#1448已确认规划的阶段1，修复半迁移、SelfService纯读争抢写锁及连接延迟释放。
基线 `c2aaf8aa`；分支 `fix/database-initialization-transactions`；候选版本1.6.1。
实现以三个聚焦提交进入PR #1464，本次仅追加文档验收回执；不合并、不部署，审查闭环由父会话接手。

已完成：

- 15个Gateway store类与节点心跳建表入口使用事务外WAL、单次LEASE和静态逐条execute；
  KnowledgeGrants与路由分离的DDL合为一段。Metrics业务SINGLE_STATEMENT保持不变。
- SelfService六个纯查询使用读连接，默认写连接及所有写方法仍持LEASE。
- 路由连接统一5秒等待、外键与finally关闭，保留CAS与跨表发布事务；Mapping独立库只修上下文关闭。
- KeyOperations旧凭据安全清理保持原语义；无新依赖、user_version或默认值归一。
- 中文[ADR0243](../adr/0243-database-initialization-transactions.md)、
  [QA计划](../acceptance/database-initialization/qa-plan.md)与Gherkin已新增。
  连接台账纳入ChatRouteBindingStore；源码守卫改为核对DDL位于initialize与禁止executescript。

验证：原版16个最小反例全红，修复后全绿；补充WAL配置失败关闭路径后共17项。
定向1109项通过；候选全量4841项通过、9项跳过；Ruff、compileall、依赖检查、wheel/sdist、
Gateway镜像与容器内导入通过。fresh、固定历史库升级、合成缺scope列兼容路径前后schema零漂移。
完整结果与初始H5基线失败见[验收记录](../validation/2026-11-11-database-initialization-transactions.md)。

父会话另提供生产只读一致副本回放摘要：33表逻辑行数与逐列摘要不变，quick_check与外键检查通过。
该证据区别于作者合成检查，不代表原入口业务验收；作者只读取已选摘要，未读取副本或原始报告。

当前缺口：store构造仍会获取写锁，单一schema/user_version/启动迁移留给#1462，不能关闭#1448。
独立Hack已完成，Codex原生默认配置执行，实测范围无发现：88个DDL成功后异常断点均回滚，
3项原版反向对照抓住旧缺陷；并发/CAS、提交失败和连接关闭等范围通过。具体模型版本不可核实。
父会话另执行真实Gateway技能，8项HTTP检查通过、临时根已清理；仅证明本地启动/鉴权/API边界。
未完成业务验收；代码审查与PR审查闭环由父会话接手。
三个提交依次为初始化与ADR0243、连接读写/释放（No-ADR引用0243）、测试文档与版本。
1.6.1交付准备时业务语义未再改动，复用已完成全量结果，重跑版本/文档/Ruff/ADR检查及构建。

操作知识自检：本轮只有本地测试、精确基础镜像拉取及隔离镜像构建，无远端运维命令；
使用的构建方法已在 deploy/m2-live-03/build-image.sh 与 .github/workflows/ci.yml 登记。

本次文档补充未修改源码：17个实现源码SHA256与原已验候选一致，版本保持1.6.1。
最小风险检查67项通过，补验非法profile拒绝与关闭后5个连接helper的32条可执行语句和8个分支全部覆盖；
Ruff复杂度检查通过。原三提交ADR检查已实际通过，详见验收记录。
