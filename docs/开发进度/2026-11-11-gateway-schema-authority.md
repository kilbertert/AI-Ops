# Gateway schema 单一权威：1.7.0交付候选

任务 #1448 / #1462；分支 `feat/gateway-schema-authority`，基线 `e60a6049`。
#1461已随1.6.1完成部署；本轮1.7.0尚未上线。已授权提交、推送和创建一个逻辑PR，
合并与部署仍由交付会话负责；Refs #1448，不关闭父故障票，#1463历史归一继续延期。

## 已完成

- 15模块中的28张Gateway表及21个具名索引统一静态声明；Mapping两表保持独立。
- 当前版构造只读版本；历史库在同一写事务内验证、补齐、复核后写version。
- 启动、Runtime、运维检查完整结构；节点心跳ensure先于原业务LEASE。
- 旧token字典读取去敏，启动按需secure_delete清理；业务TTL/授权/CAS不改。
- 新增`aiops-admin schema check/migrate --db`；标准部署脚本先一致备份和副本演练。
- 父实测SQLite3.45.1双线程WAL竞争已修：仅SQLITE_BUSY有界重试，40轮零错误。
- 独立Hacker反例修复：节点心跳显式列名单，合法物理列序变化不会反存；红灯→绿灯已留证。
- 版本四处同步1.7.0；ADR0244、Gherkin/QA与运维手册已补齐。

## 验证与缺口

详细过程和最终计数见[验收记录](../validation/2026-11-11-gateway-schema-authority.md)，
本地证据在`dist/verification/gateway-schema-authority/`。
独立Hack发现1项列序缺陷，修复后原反例与新输入复验通过；最终SQLite3.45.1一致副本检查
保持33表逻辑值，integrity正常、FK错误0。旧程序回放环境与证据时点分开记录。
最终PR CI待执行，未完成业务验收。
历史默认值和物理结构归一另属#1463，不包含公司仓库/H5修改。
下一步：按职责提交并同步最新默认分支，创建PR、等待确定性CI；合并、上线验收与关闭#1448由交付会话执行。
