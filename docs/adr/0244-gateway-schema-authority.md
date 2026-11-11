# 0244：Gateway 全库结构由单一静态声明负责

Status: Accepted

任务 #1448 / #1462，接续 ADR 0243。普通 store 构造曾每次执行 DDL，
纯查询因此等待其他业务写者；逐 store 初始化也不能代表全库结构已经完整。

## 决策

`gateway_schema` 管理 28 张 Gateway 表，MappingStore 的独立两表不纳入。
普通构造只读 `user_version`；旧库在一个 `BEGIN IMMEDIATE` 事务内校验、
建缺表、按白名单补列、建索引、复核，最后写版本。锁内重读防止两个进程重复升级。
服务启动和运维完整检查结构，不以同版本假定没有漂移。
列声明同时生成建表与检查判据；SQLite 自身解释列、索引、外键和唯一约束。
有界 CREATE 子句词法比对拒绝 PRAGMA 无法体现的 CHECK、冲突处理和额外约束，
不实现通用 SQL 解析器。只允许已知历史 `chat_dify_runs.scope_json DEFAULT ''`；
物理列序不参与比较，业务INSERT必须显式列名单。不删除额外历史表，不推断或回填授权身份。

官方依据：[SQLite 事务](https://www.sqlite.org/lang_transaction.html)、
[user_version](https://www.sqlite.org/pragma.html#pragma_user_version)、
[ALTER TABLE](https://www.sqlite.org/lang_altertable.html)、
[WAL](https://www.sqlite.org/wal.html) 与
[Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)。
事务继续用 `isolation_level=None` 和逐条 execute，兼容 Python 3.11+ / SQLite 3.45.1；
WAL 在事务外，回滚承诺针对逻辑 schema、行和版本，不承诺首次空文件或 WAL 属性消失。

旧凭据字典清理由启动显式执行，只读探测后按需拿租约并锁内重读；独立 get 仅返回 ID。
老程序回滚后可能再写旧格式，不能用 schema 版本替代数据探测。
部署在替换源码前通过 SQLite backup 保存一致副本并做候选迁移演练；
失败恢复代码，不自动用旧备份覆盖新业务写入。

## 被否掉的替代形态

- 缓存 store 可减少构造成本，但不能约束多进程、独立 CLI 和新调用者。
- 继续每 store 自迁移最少搬代码，但首个 store 无权给全库盖版本，跨表依赖仍脆弱。
- ORM / 通用迁移框架有成熟扩展能力，本轮 28 张既有表只需静态声明与一次追加迁移，
  新依赖及第二套模型不能减少风险。
- 重建全部表可以统一默认值和物理列序，但用户已选择先修故障；历史归一另属 #1463。

## 影响与边界

独立 store 首次初始化会建立全库；未知结构拒绝，需要运维先检查再决定后续修复。
词法比对有意保守：未列入合同的等价写法也可能拒绝，不能凭猜测自动改写。
业务 LEASE、Metrics 单语句写、TTL、授权时点、路由与登记 CAS 不变。
本地合成数据与一致副本不证明生产在线迁移、远端诊断或业务准确率；独立Hack复验已完成，边界见本轮验收记录。
