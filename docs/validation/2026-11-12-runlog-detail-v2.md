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
- 独立 Hack 待执行（交付前按验证分工补）。
