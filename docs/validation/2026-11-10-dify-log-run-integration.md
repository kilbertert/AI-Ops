# LOG #1459：RUN 联动、CI 修复与 1.6.0 验证

日期：2026-11-10 UTC。分支 `feat/dify-java-run-log`，起点 `557cab62`，已合入
RUN main `097aef58`。本轮版本为 1.6.0，VERSION、pyproject、包初始化及 uv.lock 同步；
父任务另行处理 COMM 1.5.0 合并，不将其他业务变更混入本轮。

## 已发生失败与处理

| 失败 | 实测根因及处理 |
|---|---|
| detail 的 failed/过期 running 用例以文件字节判只读 | 在两次读取间强制 WAL checkpoint，5 个状态用例稳定复现同一第 50 页/偏移 200708 的差异，所有表逻辑数据不变，SQL 探针未发现写操作。改为 authorizer 拒绝写 SQL、连接 total_changes 不变及完整 AgentRun 不变，保留 no-store/状态/隐私断言 |
| `test_the_exposed_set_equals_the_declared_set` | 两个新增 POST 未登记；补入实际权威 `ops/dify-exposure-registry.md`，包含当前本人/应用/租户授权与只读用途；未改变集合相等断言 |
| `test_no_second_order_grammar_literal_exists` | 精确登记日志 API 的 run_id 和 bridge 的 run_id；app_id 为既有十六进制 UUID，非该字母表计数对象。未修改正则或订单权威 |
| startup adoption | 测试替身缺少 `middleware` 装饰器导致 AttributeError；仅补替身装配能力，端/环境/目标的原业务断言保持不变 |
| Devin 所指出 trace 依赖未验证 | RUN 已合入；新增成功/失败/过期在途三种情况，使用实际 `store.record_trace` 写 SQLite，再经真实列表/详情 API 核对完整 trace、elapsed_ms、duration_ms 与状态，不构造 getattr 对象替代持久化 |

只读判据的反向检查：临时插件在日志查询前执行 `UPDATE agent_runs SET state = state`，
即使值不变也被 SQLite authorizer 拒绝，测试以 `DatabaseError: not authorized` 失败。
这证明新判据不会把真实写入放过；临时探针与变异仅存在忽略目录，未修改运行时代码。
新增长期联动检查针对评审已指出的持久化缺口；旧投影测试无法证明字段真实入库和 API 回读。

## 检查与证据

定向检查命令覆盖日志 API/bridge、RUN progress、Dify 暴露登记、纳管、订单语法台账和版本：

```bash
uv run pytest tests/dify/test_operator_run_logs.py tests/dify/test_operator_run_logs_bridge.py \
  tests/operations/test_operator_run_progress.py tests/dify/test_dify_exposure_registry.py \
  tests/dify/test_self_service_adoption.py tests/identity/test_order_identity.py tests/test_release_version.py
```

结果 **123 passed、1 skipped**，14.80 秒；skip 为既有空订单号在调用方短路，不进入 store 校验。
新增文档后的目录/README 检查另跑 25 passed；不将此项代替全仓回归。
全仓 Ruff check / format --check、uv lock --check --offline、uv pip check 通过；
wheel/sdist 成功产出 1.6.0，未新增依赖或改共享核心。
ADR 0242 补充实际持久化联动和只读判据，源决策不变。

本地忽略证据目录 `dist/verification/`：`log-write-probe.log`、`log-checkpoint-probe.log`、
`log-write-mutation.log`、`log-1.6-targeted.xml/log`、`log-1.6-ruff.log`、`log-1.6-build.log`。
制品位于 `dist/log-1.6-build/`，临时诊断探针位于 `.scratch/log-diagnosis/`。

本轮未重跑全仓 pytest、前端构建或真实 Dify/Java 整链；现有 web 成功构建记录仍只证明原候选。
没有生产访问、合并或部署；本次 PR 的远端 CI、评审和合并由父会话接续。
**未完成业务验收**。实际本地操作判据已写入[运行手册](../agents/operator-cpms/run-logs.md)。
