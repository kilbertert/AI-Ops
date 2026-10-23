# 2026-10-23 · 观察分类与统计口径的修正（#1027 之后）

## 目标

Codex 独立复核 #1027 后指出三处必修与四处事实口径错误。本片只修正这些问题，
**不扩大功能**：不新增追踪、不改业务行为、不动 41、不执行 `uv sync`。

## 已完成

### 1. 401 不再被算成服务失败

`request_observation._outcome` 原来用枚举 `{400, 403, 404, 409, 422}` 判拒绝，**漏了 401**。
生产可达形状：首次解析（`aiops:diagnoses:write`）成功，`authenticated_chat_order_reader`
里的第二次解析（`aiops:orders:read`）返回凭据无效 ⇒ 401。它被记成 `failed`，
于是"凭据过期"会抬高服务 5xx 率 —— 那正是运维判断"网关坏了"的那个数。

改为整区间判据：**任何 4xx 是拒绝，任何 5xx 是失败**。枚举正是错因，所以不再枚举。

### 2. 统计命令不再混入别的业务日志

原手册的管道直接 `grep -o 'outcome=...'`、`grep -o 'duration_ms=...'`，而网关 journal 里
知识审计有 `outcome=`、Dify 试运行有 `duration_ms=`。合成混合日志实测：本片 1 条结果
被报成 2 条，10 ms 被报成 10/9000。

现在每条命令的第一步都是筛到 `assistant_question_result`，并固定观察窗口；
目标租户过滤只用于汇总，**「未登记租户」告警单独全租户查**（否则会被目标租户过滤吞掉）。
「读取失败 / 零样本 / 有样本」三种状态分开写。

### 3. 模型调用的归因口径

`agent_run_metrics` 与 `runs/` 都**没有 `qa_id`**，所以"同租户同秒出现 `routing` 行"
不能证明是这一条查询调用了模型 —— 共享网关上的普通问答本来就会写同样的行。
按那个口径设停止条件，会把别人的正常问答记成本片事故。

改为：**有真实关联键才归因**；对不上就写「待核实」，并附窗口内候选行数与 `routing` 行数。
**不新增全链路追踪。**

### 4. 四处事实口径

| 原写法 | 实际 |
|---|---|
| `qa_id` 仅成功时有值 | 证据落盘失败（503）时 QA 行已经建出来，`qa_id` 有值但结果是 `failed`；那个关联要留着 |
| HTTP 200 = 查到订单 | 200 clarification 的 `qa_id` 是 `-`，**没有查到订单**，只是要求补订单号 |
| 401/422 都有行 | 只有**候选形成之后**的 4xx 有行；初始身份/请求校验失败没有这一行 |
| outcome 四类 | **只有三类**：`completed` / `refused` / `failed` |

## 验证

| 项 | 命令 | 结果 |
|---|---|---|
| 既有回归 | `uv run pytest -n 0 tests/test_consumer_order_chat.py tests/test_agent_metrics.py` | **56 passed**（未改断言） |
| 新增 | `uv run pytest -n 0 tests/test_request_observation.py` | **23 passed**（原 18 + 新 5） |
| 合计 | 三者一起 | **79 passed** |
| 真实 HTTP | `observe_consumer_order_result.py` | **23/23 检查通过**，11 条结果行（成功 5 / 拒绝 4 / 失败 2） |
| 静态检查 | `ruff check .` / `ruff format --check .` | 干净 |

新增的 5 条测试各自钉住一处修正：

- `test_revoked_credential_on_re_authentication_is_a_refusal_not_a_failure`（真实 API 路径，
  断言两次解析的 scope 顺序与 `outcome=refused`）；
- `test_every_4xx_is_a_refusal_and_every_5xx_is_a_failure`（区间边界：401/429 是拒绝，5xx 不是）；
- `test_outcome_is_exactly_one_of_three_values`；
- `test_evidence_failure_keeps_the_real_qa_id`（503 仍保留 `qa_id`）；
- `test_a_clarification_is_a_200_without_an_order`（200 + `qa_id=-` ≠ 查到订单）。

## 反向验证

把 `_is_refusal_status` 改回"排除 401"（即旧枚举的缺口）⇒
**2 条转红**（重认证 401 用例 + 区间边界用例）；还原后 79 passed。

## 已知缺口（未在本片处理）

- 端到端时延（不含 nginx 与网络）、按 `request_id` 跨层串联 —— 仍判不了；
- 其他入口（`GET /v1/orders/{no}/charging-status`、M3 委派）没有等价观察；
- **已合并但尚未部署**：CD 因 `pyproject.toml` 摘要漂移拒绝（来自 #1023 的 pytest
  `addopts`），`uv.lock` 两侧逐字相同，没有依赖需要安装。生产仍是 `0.1.0+26bc23a34fd4`，
  本模块在生产上不存在。部署处置由 Codex 核验与准备。

## 下一步

等部署阻塞处置完成后，用观察契约第六节的命令在 41 上确认能看到
`assistant_question_result` 行；看不到就说明制品还是旧的。
