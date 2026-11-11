# 数据库初始化第一阶段 QA 计划（#1461，Refs #1448）

范围：已确认规划的阶段1，基线 `c2aaf8aa`。判据来自用户确认的事务/连接合同与
[Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Cursor.executescript)、
[SQLite 事务](https://www.sqlite.org/lang_transaction.html)、[WAL](https://www.sqlite.org/wal.html)。

## 修改前观察（2026-11-11）

- Gateway 的14个源码模块（15个 store 类）执行 `executescript`；当前 legacy 事务控制会先提交
  已开启的事务，之后的补列因此不能由外层回滚。KnowledgeGrants、ChatRouteBindingStore
  的建表和补列另开连接。Metrics 初始化与业务写入共用 SINGLE_STATEMENT。
- SelfServiceStore 的 request/get/recent/app/apps/consumer_target 只有 SELECT，连接却恒为写租约。
- ChatRouteBindingStore 原连接是30秒等待，未开启外键，也未显式关闭；CAS 谓词与跨表写已有实现。
- MappingStore 使用独立 mappings.db；原 `with sqlite3.Connection` 提交/回滚但不关闭。
- KeyOperations 建表后另有 secure_delete 与旧 token 字典清理；保留该安全维护及新旧数据含义。
- 构造、连接、调用者检索留在 `dist/verification/database-initialization/callers.txt`。
  全部结构修改前由实际构造入口采集（包含心跳与独立 Mapping）；28张 Gateway 业务表、
  2张 Mapping 业务表，另各含 sqlite_sequence，不能把内部表当新增业务表。

## 判据和执行

| ID | 入口与输入 | 独立预期 |
|---|---|---|
| INIT-01 | 固定历史提交9080a463的 chat_dify_runs 表与合成行；中途补列 execute 抛错 | sqlite_schema 与逻辑行保持原样；重试成功，历史行身份不变 |
| INIT-02 | Gateway/Metrics 中途建表或索引失败；Knowledge/Route 补列失败 | 整批结构回滚，不留下第一张表或前段DDL |
| READ-01 | 已构造 SelfServiceStore，另一连接持 BEGIN IMMEDIATE；调用六个纯读方法 | 返回已提交行，不申请写锁；写连接默认仍持租约 |
| CLOSE-01 | Mapping/Route 连续读、异常退出；连接配置 PRAGMA 失败 | 连接立即关闭；写失败回滚，成功提交，不依赖GC |
| SCHEMA-01 | 基线与候选实际初始化；旧表升级；全部表、列、索引与约束逐项比较 | 无结构漂移；scope_json fresh无默认、兼容缺列路径默认空串仍不同 |
| WRITE-01 | 现有 CAS、权限/TTL/KeyOperations 清理测试及临时跨表失败探针 | 原拒绝、回滚、清理与时间语义保持 |

新增长期检查仅针对已复现的半迁移、读锁和连接释放；现有连接参数、业务回滚与源语法守卫
没有覆盖初始化失败和上下文退出关闭，因此需要最小反例。结构快照只做本轮对照，不将候选
声明生成的输出当作历史判据。Gherkin 见 [scenarios.feature](scenarios.feature)。

执行：`uv run ruff check .`、`uv run ruff format --check .`；定向 scalar_rules、
chat_route_bindings、dify 及受影响调用者；`uv run pytest`；`uv build`；镜像构建。
证据写入 `dist/verification/database-initialization/`，不含生产数据或凭据。
本阶段不改变HTTP合同；父会话已执行临时Gateway的8项HTTP检查，独立Hack实测范围无发现，详见本阶段验收记录。

父会话已提供只读一致副本回放摘要，见本阶段验收记录；不替代上述作者合成检查或真实业务旅程。
未验：真实模型/KB、原App/H5业务旅程与业务准确率。未完成业务验收。
构造持锁、user_version、统一schema与启动治理属于#1462；本阶段不能关闭#1448。
