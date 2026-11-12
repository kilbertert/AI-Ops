# 管家运行日志扩边：投影与明细（2026-11-12）

任务分支 `feat/operator-run-log-detail`，本地 main 基线 `d41ff340`。
用户批准将 ADR-0242 日志白名单拓宽为「执行元数据 + 问题全文 + 绑定订单号」，
新增 [ADR-0249](../adr/0249-run-log-detail-projection.md) 记录边界修订；
答案、证据 data、工具参数、凭据、scope 指纹、自由文本失败原文仍不投影。

## 改动

- `execution/agent_runs.record_trace`：trace 白名单加 `detail`（机器码），
  存储层 `str(...)[:64]` 截断兜底。
- `operator/operator_cpms._trace`：工具拒绝携带拒绝码、复核拒绝携带原因、
  重采样携带 dispatch_refused/no_answer_accepted、终态失败携带 `_failure_code`。
- `operator/operator_cpms.start`：把本轮 `language` 传入 `AgentRunStore.issue`
  （Devin Review 发现：此前该列恒 NULL，新字段永远显示「未记录」）。
- `execution/operator_run_logs`：投影扩入 reason(≤128)/question/order_no/节点对/
  environment/language/观测版本与核对结论/领取·完成·过期时间/结果指纹/
  trial（服务端按 chat_binding `operator-trial:` 前缀派生，绑定串不投影）/
  历史轮数/声明集内证据工具名；问题经同一只读连接按
  `qa_id + scope_fingerprint` 批量读（绕开写入式 `get_assistant_question`）。
- `deploy/dify-36/web-patches/1.17.1/aiops-operator-run-logs.tsx`：列表加
  问题/订单号列；详情分块展示终态原因、执行节点（不一致标注）、时间线、
  版本核对、结果指纹、证据工具与逐事件拒绝码；新字段全部可选兜底。

## 确定性检查

- `uv run pytest tests/dify/ tests/operations/ tests/gateway/ tests/common/`：
  全绿（含只读 SQLite authorizer + total_changes 判据覆盖问题批量读的 SELECT）。
- `tests/dify/test_operator_run_logs.py`：更新精确字段集断言、PRIVATE 哨兵
  （答案/证据 data/注入 evidence 键仍不透出）、新增问题投影与异指纹错绑
  隔离、注入 evidence 键过滤、chat_binding 派生 trial、detail 存储层截断。
- `verify-aiops-gateway`（一次性数据根）：全项通过，
  `dist/verification/gateway/aiops-gateway.json`。
- `node .sandcastle/adr.check.mjs --base origin/main`：通过（0249 号无撞号）。
- ruff check/format：通过。

## 独立 Hack（deepseek-v4.1-flash，两轮）

- 第一轮（回执 `/tmp/aiops-hack-runlog-detail-result.json`，私有）：
  3 medium + 1 low 全部实测复现——
  ① 死 run 取证路径 evidence 键可被节点注入任意文本（已改过滤声明集）；
  ② `trial` 取快照键不可靠（已改 `chat_binding` 前缀判据）；
  ③ `reason[:80]` 截断 115 字符合法码（已改 128）；
  ④ `detail` 上限只在 `_trace`（已下沉 `record_trace`）。
  判「未发现逃逸」面：异指纹错绑问题、reason 自由文本、复核 reason 封闭集、
  chat_binding 形状、`str(exc)` 上游正文。只读判据三轮复跑通过。
- 第二轮并行探索（同回执文件后半，跑在修复中版本上）：复测确认注入键、
  reason 截断、detail 无 store 截断三项已修复；自写 authorizer + total_changes
  探针覆盖问题 SELECT，畸形快照形状全部安全降级，相关套件 1712 passed。
  新发现三条——① 前端 `brief`/`stamp` 形参在 strict +
  `noUncheckedIndexedAccess` 下收 `undefined` 实参报 TS2345（已放宽签名为
  `string | null | undefined`）；② `chat_binding` 原样投影与「不投影内部
  app/agent 标识」自相矛盾（已从投影与前端移除，绑定串仅服务端派生 trial）；
  ③ 码表缺口（已补 `ttl_elapsed`/`cancelled_*`/`retracted_by_operator` 等并加
  `operator_node_failed: <子码>` 前缀回退）。性能备注：列表每行 `receipt()`
  对快照做 SHA-256，400KB 快照约 1.6ms/行——分页 ≤50 行，可接受不优化。
- 第三轮复验（`bdb73lebq`，修复后版本，回执 `/tmp/aiops-hack-runlog-recheck-result.json`）：
  4 项修复经真实 `fail→absorb→read_runs` 路径全部 PASS（含 127/128/129 字符
  边界与非字符串 detail）；新增 2 low——① 死 run 取证仍可写「声明内但未实际
  取过」的工具名：泄露面已关，剩审计保真度，dead 分支 task 常已不在内存、
  服务端无可靠判据，如实记入 ADR-0249 口径注记不改代码；② 本文件一行过时
  `chat_binding` 字段（已修）。`answer_review` 非 dict 的 AttributeError 属
  base 既有行且节点不可注入，不记本次缺陷。

## 未验范围

前端 vitest/tsc 未在本机跑（dify web 构建环境在交付链路执行，且上游
`typescript.ignoreBuildErrors`）；Hack 人工核对了缺字段兜底不崩。
真实 Dify/Java 端到端（节点回调实际携带 detail 的事件流）由发布后验收。
**未完成业务验收**：本记录只覆盖本地 API 合同与合成数据验证，真实业务运行
（真实管家问答在新日志页呈现问题/订单号/节点明细）待发布后另行验收。
