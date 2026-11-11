# #1461 验证：初始化事务与连接边界（#1448阶段1）

日期：2026-11-11；基线 `c2aaf8aa`；候选分支 `fix/database-initialization-transactions`，交付版本1.6.1。
作者环境Python 3.13.13、SQLite 3.51.2，只操作一次性数据库；父会话副本摘要单列于下。
证据目录：`dist/verification/database-initialization/`（本地忽略目录，父会话交付前保留）。
判据与修改前观察见[QA计划](../acceptance/database-initialization/qa-plan.md)。

## 红灯与修复

最小反例在改源码前运行：`uv run pytest tests/gateway/test_database_initialization.py
 tests/dify/test_store_connection_boundaries.py`，`red.log`记录16项失败。

- 固定历史提交9080a463中的chat_dify_runs DDL与合成行，第二次补列execute抛错，原版留下前一列及索引。
  比较sqlite_schema与逻辑行，未用DB文件hash。修复后整批回滚并可重试，行身份/旧scope保持。
- Gateway第二张表、Metrics后续索引、Knowledge/Route补列被SQLite authorizer拒绝，原版留下部分结构。
- SelfService六个SELECT入口在另一连接持写锁时均失败；Mapping/Route查询连接与PRAGMA配置失败未关闭。

最初相同16项修复后通过（`green.log`）；随后增加journal_mode失败路径，最终17项随定向和全量通过。
新增长期检查只针对已证实的持久化、读锁与关闭边界；既有连接参数与业务回滚检查缺少这些初始化/退出判据。

## 命令与结果

| 命令/入口 | 修改前 | 候选 | 证据 |
|---|---|---|---|
| `uv run ruff check .` / `ruff format --check .` | 通过 | 通过 | before/after-ruff-*.log |
| `uv run pytest tests/common/test_scalar_rules.py tests/execution/test_chat_route_bindings.py tests/dify` | 984 passed | 加入新回归、旧表/健康/快捷动作/目录检查，共1109 passed | before/after-targeted.log |
| `uv run pytest` | 4823 passed、1 failed、9 skipped；仅排除尚未修复的新反例 | 4841 passed、9 skipped | before/after-full.log |
| `uv run python -m compileall -q src tests` / `uv pip check` | — | 通过，40包相容 | compile.log、pip-check.log |
| `uv build --out-dir <本轮证据>/before-build或after-build` | wheel/sdist通过 | wheel/sdist通过 | before/after-build.log |
| `deploy/m2-live-03/build-image.sh`，本轮专用M2LIVE_GATEWAY_IMAGE与M2LIVE_DIST | 通过 | 通过 | before/after-image.log、对应gateway-image/identity.json |
| 容器 `--network none --read-only --entrypoint python` 导入Gateway | — | 通过 | image-import.log |
| `node .sandcastle/adr.check.mjs` | — | 结构报告无0243问题；旧ADR建议仍报告 | adr-structure-report.log |
| `node .sandcastle/adr.check.mjs --base origin/main` | — | 退出0，但范围0提交，**不算逐提交覆盖验收** | adr-check.log |
| `dev-worktree audit` | — | 通过；本地台账单独保留 | 本地交接记录 |

构建首次因本地无锁定基础镜像失败；按既有CI方法拉取精确digest后重跑成功（image-base-pull.log）。
首次实现阶段（1.6.0元数据）的候选镜像ID `sha256:ae92a9bdc41a659cf37bdc8c764bfef7e98836e210f3bc1f447a5930adedc0a4`，
源上下文摘要 `d357d56105b1eb586ab4404b88ed4d9c405a94b2bc591cae1414bac352d285af`。
制品只用于本地验证，不表示已部署。

全量修改前的失败为 `tests/test_h5_assistant_routes.py::test_every_declared_path_is_forwarded`，
13条声明路径未转发；候选全量该项通过，H5/Nginx相关源码与断言没有改动。
隔离基线源码复验该文件3项通过（baseline-h5-recheck.log），说明首次失败未稳定复现；
根因未定位，不将其转绿归因于本修复。保留原失败证据，不删除/放宽断言。

## Schema与业务边界证据

- `schema_snapshot.py`驱动每个构造入口、节点心跳和独立Mapping库，采集sqlite_master.sql、
  table_info、foreign_key_list、index_list/index_xinfo：28张Gateway与2张Mapping业务表，另各含sqlite_sequence。
- fresh快照在修改前采集；历史对照通过 `git archive c2aaf8aa src` 隔离原程序后运行相同探针。
  历史DDL来自9080a463（Dify）与44260454（Agent）。三组before/after均逐项完全一致，
  包括列顺序、默认值、主键、唯一/外键约束、索引排序/表达式；结果 `schema-comparison.json`。
- 缺scope_json的第三组明确为合成兼容检查，**不冒充真实历史版本**：fresh与真实历史DDL的该列无默认，
  缺列路径仍DEFAULT ''；没有归一、回填或重建。`ddl-comparison.txt`另核对45条旧DDL语句保持原样。
- `behavior_probe.py`实际双线程同时CAS，结果一个published、一个ROUTE_BINDING_CONFLICT；
  登记表触发器抛错后路由/登记共同回滚；通过路由写连接插入孤儿agent_version被现有外键拒绝。
- 同探针：另一连接持写锁时app读取0.4ms；gc.disable下Mapping和Route各30次读取，fd增长均0。
  默认SelfService写连接仍在事务中。完整输出 `behavior-probe.json`。
- 原权限/TTL、发布/撤回、旧KeyOperations字典去敏均由已有定向/全量测试覆盖。

## 父会话只读一致副本回放摘要

来源：父会话选定的第一阶段证据摘要，作者只读该摘要，未读取数据库副本或原始JSON报告。
父会话通过SQLite backup取得一致副本，在副本运行当前第一阶段源码的完整store初始化集合。

- 原库28张受管表的列、约束与索引语义和新建基线一致，5张历史表保留。
- 初始化前后33张表的逻辑行数与逐列摘要不变，未把文件字节变化当数据漂移。
- `quick_check=ok`，`foreign_key_check=0`。

这是父会话的生产副本回放证据，与上节作者合成检查分别归档；它证明该副本初始化保留逻辑数据，
不证明原入口业务行为、业务准确率或所有历史数据库形状。原始库和原始报告不进入公开材料。

## 1.6.1交付准备

四处版本声明（VERSION、pyproject、运行时包版本与uv.lock的本包条目）同步到1.6.1。
本轮不改业务语义，沿用上述全量4841 passed、9 skipped及schema对照结果，不重复全量套件。
版本/文档检查38 passed，Ruff检查与格式检查通过，1.6.1 wheel/sdist构建通过。
证据为delivery/version-doc-tests.log、delivery/ruff-*.log与delivery/build.log。
初始化提交0eb2a6c8与连接提交2e603917已分别通过逐提交ADR检查；
最终测试/文档/版本提交的覆盖检查与PR checks状态在交接时据实记录。
首次实现的镜像证据仅对应当时1.6.0元数据，不冒充1.6.1镜像制品。

## 未验与交接

未完成业务验收。真实模型诊断、KB活性、原App/H5业务旅程尚未验证。
独立Hack正在进行、待回执；作者自查与父会话副本回放均不冒充独立Hack结论。
本阶段通过#1461交付，Refs #1448；store构造锁及统一schema/version升级留给#1462，不能关闭父issue。
本轮仅提交、推送及创建一个PR，不合并/部署；PR检查状态及后续审查闭环由父会话接手。

本轮运维方法自检仅涉及本地docker pull/build/run；已有构建脚本与CI命令可复用，无新增远端操作手册。
最终文档补齐后目录/README架构检查结果见final-doc-checks.log。
本地Git/工作树交接台账单独保留，不进入公开材料。
