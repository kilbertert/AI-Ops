# 运行日志行级明细（ADR-0251）验证记录

- 日期：2026-11-12
- 里程碑：运行日志「人话行 + 入参/结果摘要 + 派发与复核明细」
- 分支：`feat/operator-runlog-detail-v2`

## 判据与方法

1. **store 边界**：`record_tool_summary` 拒非法工具名/超字段数/脏键；标量截 32 字、列表记 `N项`、字典压缩 JSON 截 32 字；≤16 工具后写覆盖、同工具覆盖。（`tests/execution/test_agent_run_records.py`）
2. **投影**：detail 响应带 `tool_summaries`；无摘要 run 为 `null`；异 scope 摘要不可见（`PRIVATE` 全文断言）。（`tests/dify/test_operator_run_logs.py`）
3. **发射点**：派发 `tools=N`、复核通过 `allowed`、工具成功/拒绝均写摘要（`tests/operations/test_operator_run_progress.py` 断言键集合仍限白名单）。
4. **schema**：`VERSION` 升 2，`tool_summaries_json` 入 `LEGACY_COLUMNS`；`tests/gateway/test_gateway_schema.py` 36 项全绿（含 pinned 历史库迁移）。
5. **网关启动验收**：`verify-aiops-gateway` 一次性数据根全绿（`dist/verification/gateway/aiops-gateway.json`）。
6. **前端**：`aiops-operator-run-logs.browser.spec.tsx` 按新行格式更新（整句+摘要行断言）；vitest 需在 web 构建环境跑，本机未执行——构建期 webpack 编译即校验。

## 验收状态

- **未完成业务验收**：无真实 CPMS/节点链路跑过；行级内容正确性以 store/投影/发射点单测与启动验收为证。
- 结果摘要字段来自 `task.evidence[tool].data` 白名单表，未对全部 11 个工具做线上形状核对——未覆盖的工具表现为「该行无摘要」而非报错。
- **独立 Hack 已执行**（回执 `/tmp/aiops-hack-runlog-v2-result.json`，本机 73 项触及测试绿）：
  - 🔴 v1→v2 迁移被拒：`user_version=1` 的现网库在 `ensure()` 只读快路径直接
    `SchemaError`，部署即启动阻断——已修（`_version` 接受 `0..VERSION`，
    `check` 对 `version < VERSION` 走 legacy），并新增 v1 形状迁移测试。
  - 🟡 摘要写入的 `TypeError`（不可序列化字典成员/非对象列形状）逃出
    `_summarize_tool` 的吞错面进业务路径——已修（`default=str` 降格、
    `_tool_summaries` 形状守卫、except 补 `TypeError`），新增加固测试。
  - 🟡 非对象 `execution_manifest` 以 `AttributeError` 逃逸——已修（isinstance 守卫）。
  - 复核确认无问题面：scope 闸、16 工具上界与覆盖语义、拒绝码全为机器码、
    `_arg_summary` 边界与声明输入过滤、BEGIN IMMEDIATE 串行化、前端转义与
    `tools=\d+` 严格解析。
- Devin 复审同轮另抓到：同名覆盖摘要误挂历史事件（已改只挂最后一次完成事件）、
  工具行丢累计耗时（已恢复「耗时+累计」双呈现）、run-logs.md 边界说明（已同步）。
