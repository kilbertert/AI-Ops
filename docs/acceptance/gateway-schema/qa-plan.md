# Gateway schema 验收计划

判据来源：实施前已确认的 #1448 / #1462 分阶段计划、初始任务说明，
固定历史 9080a463（Dify）与 44260454（Agent），SQLite 官方事务/PRAGMA/WAL/backup 合同。
本文件是实现后的判据归档，不声称它在代码修改前新建；执行结果见
[本轮验收记录](../../validation/2026-11-11-gateway-schema-authority.md)。

| ID | 前置 / 入口 | 步骤与独立预期 | 清理 / 未验范围 |
|---|---|---|---|
| DB-01 | 当前版临时库，外部进程 BEGIN IMMEDIATE | 所有store新构造并纯读；总耗时<1s，trace无BEGIN/DDL/UPDATE | 子进程退出；真正到期更新仍保留LEASE |
| DB-02 | 固定历史DDL+合成行 | 建表/补列/索引/写版本后异常；逻辑schema/行/version逐项不变 | 临时库自动清理，不比较文件字节 |
| DB-03 | 两进程在快检后屏障同步，空库/旧库 | 两者成功且只有一次写版本；40轮双线程空库无BUSY失败 | 有界join，不修改业务重试 |
| DB-04 | future/unknown，同版缺列/索引/约束变化 | check/startup明确拒绝且逻辑库不变；check缺文件不得创建 | 包括CHECK、排序、collation、partial、索引名冲突 |
| DB-05/06 | pinned DDL和合成身份/行值 | 旧身份不回填；完整结构与fresh一致，只允许scope_json默认空串例外 | 不比较cid/列顺序；历史归一属#1463 |
| DB-07 | 独立route store，无其他store预热 | source_agent_name发布成功；旧revision拒绝；registry写后异常全部回滚 | 沿用既有CAS行为套件 |
| DB-07a | 物理node_id列在environment之前的固定DDL | startup通过；record按环境/节点读回正确payload；再次record更新同一行 | 独立Hacker反例，红灯后列名单修复 |
| DB-08 | Mapping独立库、原连接释放套件 | 仅两张业务表、version仍0、无Gateway副作用；连接不靠GC关闭 | SQLite内建sqlite_sequence不算业务表 |
| DB-09 | 临时Gateway、TTL/权限已有套件 | 启动、401鉴权、公开OpenAPI、退役404；已撤权/过期不复活 | 本地不证明Dify/Java、KB和业务准确率 |
| DB-10 | WAL合成库、部署真实脚本块 | SQLite backup保留WAL行；副本migrate/check/integrity/FK；失败不切源码 | 不覆盖源库；一致副本和旧程序回放已由交付会话验；不代表在线升级 |
| SEC-01 | 当前版再次写旧token字典 | get只返回ID；启动clean_legacy按需写、secure_delete保留 | 合成值，不记录真实token |
| MUT-01 | 候选临时拷贝 | 提前version、删锁内重读、漏trace_json补列三个变异必须红灯 | 不改工作树，不提交变异 |

作者入口：`uv run pytest tests/gateway/test_gateway_schema.py tests/gateway/test_database_initialization.py`；
部署入口：`uv run pytest tests/test_cd_deploy_scripts.py`（dev-host替身，不接触生产）；
临时服务：`uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --port 0 --out dist/verification/gateway-schema-authority/gateway`。
证据统一保存 `dist/verification/gateway-schema-authority/`；父会话独立Hack不算作者自查。
