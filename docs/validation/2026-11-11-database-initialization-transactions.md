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
| `node .sandcastle/adr.check.mjs --base origin/main` | 首次实现未提交，范围0，不算覆盖验收 | 三提交范围检查实际通过，1项No-ADR豁免被识别 | delivery/adr-commit-3.log |
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
初始化提交0eb2a6c8、连接提交2e603917及测试/文档/版本提交bcbdf5f1完成后，
已实际执行 `node .sandcastle/adr.check.mjs --base origin/main`，退出码0：
`coverage judged 3 commit(s)`、`1 waived`、`adr check ok (232 record(s))`。
证据为delivery/adr-commit-3.log，本次同范围复核也通过（hack-followup/adr-three-commits.log）。
这里记录的是逐提交ADR覆盖，不代表Python测试覆盖；PR checks另由GitHub报告。
首次实现的镜像证据仅对应当时1.6.0元数据，不冒充1.6.1镜像制品。

## 独立Hack与临时Gateway回执（2026-11-11）

独立执行者为Codex原生默认配置，未切换模型；没有可独立核对的具体模型版本字符串，
不能写成DeepSeek或猜测模型名。执行时间02:46:18–02:57:05 UTC，Python 3.13.13 / SQLite 3.51.2。
结论限定为**已实际执行的范围内未发现候选缺陷**，和作者合成检查、父会话副本回放分别记录。

- 15个Gateway store的88个DDL成功后异常断点全部回滚到原schema和逻辑行，连接立即关闭、重试成功；
  Mapping保留原独立事务约定，不在此原子DDL矩阵内。
- 原版负向对照3项均抓住旧缺陷：历史补列留下部分schema、SelfService读锁等待失败、Mapping上下文退出未关闭。
- 12线程路由CAS为1胜11拒；跨表发布失败回滚，8并发claim仅1个成功；15个store同库并发初始化通过。
- RuntimeError/KeyboardInterrupt/SystemExit、PRAGMA/row_factory及延迟外键提交失败等退出路径回滚并关闭；
  Mapping/Route各500次读取fd增长0。Route锁等待约5秒后拒绝，纯读仍可进行。
- 固定历史DDL、结构对照、scope_json默认差异、TTL/状态、KeyOperations安全清理、心跳失败回滚均有实际输入；
  既有两个回归文件17项通过。并发仅覆盖报告中的调度，不声称穷尽。

Hack未读取生产或副本，未验真实业务、断电/磁盘损坏/磁盘满、其他Python/SQLite版本或全部历史形状。
父会话已核对候选diff；本次再核对17个实现源码SHA256与交付前清单一致，文档更新不改变代码哈希。
1.6.0到1.6.1只更新版本元数据，业务实现不变；不把新版本号等同新增业务验证。

父会话执行命令：

```bash
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/database-initialization/gateway
```

已核对gateway/aiops-gateway.json：8项HTTP检查通过——health自报1.6.1，两个受保护入口无令牌返回401，
OpenAPI返回200，四个退役/未开放入口返回404。临时根已由父会话清理。
该结果仅证明本地真实进程的启动、鉴权与API边界，不证明模型诊断、KB或原App/H5业务旅程。

## 最小风险核对

本仓已声明pytest-cov并安装coverage，故对5个改动连接helper运行分支覆盖；没有为本次文档变更新增长期测试。
执行命令（覆盖数据与JSON均留在忽略的验收目录）：

```bash
COVERAGE_FILE=dist/verification/database-initialization/hack-followup/.coverage uv run pytest \
  tests/gateway/test_database_initialization.py tests/dify/test_store_connection_boundaries.py \
  tests/common/test_scalar_rules.py --cov=aiops_diagnostics.gateway.gateway_db \
  --cov=aiops_diagnostics.dify.dify_login_service --cov=aiops_diagnostics.dify.self_service_store \
  --cov=aiops_diagnostics.execution.chat_route_bindings --cov-branch \
  --cov-report=json:dist/verification/database-initialization/hack-followup/coverage.json
```

实际67项通过。首次覆盖缺少非法profile拒绝分支，临时断言补验拒绝且连接已关闭后合并coverage数据：
5个helper共32条可执行语句、8个分支全部覆盖；指标只针对这些helper，不代表整个store模块覆盖率。
Ruff C901按复杂度上限10检查这5个helper通过；提取函数的临时副本不改候选源码。
证据：hack-followup/risk-coverage.log、helper-coverage-summary.json、profile-refusal.log、helper-complexity.log。

项目未声明/安装mutmut、cosmic-ray等mutation框架；本轮不引入工具。持久化风险采用独立Hack的
88处成功后故障注入与3项原版反向对照作为替代证据，**没有运行mutation框架或计算mutation score**。
文档/目录检查25项通过，Ruff与ADR检查通过。本次只补文档和风险证据，不重复全量或镜像；
原有未验边界继续保留。

## 未验与交接

未完成业务验收。真实模型诊断、KB活性、原App/H5业务旅程尚未验证。
独立Hack已在上述实际范围完成且无发现；父会话已读完整报告并核对候选diff，负责Devin线程处置与后续交付。
本阶段通过#1461交付，Refs #1448；store构造锁及统一schema/version升级留给#1462，不能关闭父issue。
本轮仅提交、推送及创建一个PR，不合并/部署；PR检查状态及后续审查闭环由父会话接手。

本轮运维方法自检仅涉及本地docker pull/build/run；已有构建脚本与CI命令可复用，无新增远端操作手册。
最终文档补齐后目录/README架构检查结果见final-doc-checks.log。
本地Git/工作树交接台账单独保留，不进入公开材料。
