# 243. 初始化逐条执行DDL，读租约与连接寿命显式化

Status: Accepted

#1461 实现#1448已确认规划的第一阶段；用户选择先修故障、历史默认值与约束归一后置。
统一schema权威与版本化升级仍在本任务下一阶段，由#1462承接。
当前 legacy 事务控制下，`executescript` 会先提交已挂起事务，导致后续补列失败仍留下前半结构。
SelfService 纯查询误拿写锁、Mapping 与路由连接依赖GC释放也已在一次性库复现。

## 决定与依据

- `gateway_db.initialize(path)` 在普通连接设置 WAL并关闭，再持一个 LEASE 写事务。
  每个 store 将原DDL静态列成完整 `execute` 语句，补列及依赖它的索引处于同一事务；
  KnowledgeGrants 与路由合并分开的DDL块。Metrics 业务继续 SINGLE_STATEMENT，初始化使用LEASE。
- [Python sqlite3官方说明](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Cursor.executescript)
  中的隐式COMMIT限定于 LEGACY_TRANSACTION_CONTROL；本库使用该默认事务控制配合
  `isolation_level=None`，后者不取消已显式开始的事务。无需改sqlite全局或Python连接模式。
  [SQLite事务](https://www.sqlite.org/lang_transaction.html)允许事务内DDL回滚；
  [journal_mode](https://www.sqlite.org/pragma.html#pragma_journal_mode)不能在事务中切换。
  回滚承诺是schema和逻辑行，不是WAL属性、空文件或文件字节恢复。
- SelfService `connection(write=True)` 保持默认兼容，六个纯SELECT方法显式 `write=False`；
  到期清理、读后写、claim/recover等继续写租约。没有把所有GET当纯读。
- ChatRouteBindingStore 纳入 gateway_db 的5秒、外键、事务与 finally close 约定，保留SQL中的
  CAS谓词与路由/应用登记跨表写。历史配置允许单独文件不代表受管节点场景与Gateway分库。
- MappingStore 继续独立 mappings.db 与默认sqlite事务模式，只在原上下文外增加 finally close；
  不接入WAL、全库schema或新版本号。连接参数PRAGMA失败也必须关闭。
- KeyOperations 的 secure_delete 与旧token字典去敏步骤保持原语义；不推迟到普通请求之后。

## 被否掉的替代形态

- 给 `executescript` 再包一层事务：改动最少，但legacy隐式COMMIT仍会打断它，不能修复半迁移。
- 运行时按分号拆SQL：能少改DDL文本，但字符串、注释和触发器中的分号并不是语句边界，
  本轮只需静态SQL，解析器增加不必要的运行时合同。
- 立即统一schema、user_version与默认值：可同时消除构造锁，却要定义全库识别、升级及启动合同；
  属于已确认阶段2。现阶段保留scope_json历史默认差异，不回填身份或重建表。
- 将Mapping一起纳入Gateway：连接代码更统一，但改变独立文件及事务约定，超过已证实关闭缺陷。

## 后果与验证边界

初始化失败可整批回滚，已构造SelfService对象的纯查询可与WAL写者并行；其余store构造仍拿写锁，
构造免锁与统一结构治理留给#1462，不能据此关闭#1448。路由现在至多等待5秒，调用方更早收到锁失败；不重试或掩盖该错误。
外键打开后新写入违反已有约束会被拒绝；不扫描、删改既有孤儿行，也不新加schema约束。

固定历史DDL、故障注入、连接关闭与原有业务回归见
[QA计划](../acceptance/database-initialization/qa-plan.md)。全结构前后对照和红绿证据留本地验收目录。
副本回放、独立Hack及临时Gateway HTTP验证分别记录在本阶段验收文件。
独立Hack在实测范围内无发现；真实业务尚未验收。
