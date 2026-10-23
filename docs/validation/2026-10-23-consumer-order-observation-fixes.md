# 2026-10-23 · 观察分类与统计口径修正：验收记录

基线：#1027（`9a699fb`）合并后的 Codex 独立复核指出的三处必修与四处口径错误。
本片只修正它们，不扩大功能。

范围声明：本片只声明**观察分类与统计口径**的本地验证范围；业务验收沿用
[#1015](../agents/consumer-order-chat/real-h5-status-acceptance.md) 与 [#1027](2026-10-23-consumer-order-observability.md) 的结论，本片未新增真实业务验收。

## 修正一：401 被当成服务失败

**缺陷**：`_outcome` 用枚举 `{400,403,404,409,422}` 判拒绝，漏了 **401**。

**生产可达路径**（不是构造边界）：首次解析 `aiops:diagnoses:write` 成功，
`authenticated_chat_order_reader` 内的第二次解析 `aiops:orders:read` 凭据无效 ⇒ 401。
被记成 `failed` ⇒ 凭据问题抬高服务 5xx 率。

**复现（修前）**：

```
status: 401 | body: {'error': {'code': 'INVALID_ACCESS_TOKEN', ...}}
assistant_question_result ... outcome=failed http_status=401 error_code=INVALID_ACCESS_TOKEN
```

**修后**：同一路径同一请求 ⇒ `outcome=refused http_status=401`。

**判据改成区间**：任何 4xx = 拒绝，任何 5xx = 失败。枚举正是错因，所以不再枚举，
测试钉的是边界（401/429 是拒绝、5xx 不是）而不是集合成员。

### 回归

`test_revoked_credential_on_re_authentication_is_a_refusal_not_a_failure` 驱动真实 API 路径，
断言：401、`outcome=refused`、`http_status < 500`、两次解析顺序
`["aiops:diagnoses:write", "aiops:orders:read"]`。

## 修正二：统计命令混入其他业务日志

**缺陷**：手册的统计管道直接 `grep -o 'outcome=...'` / `grep -o 'duration_ms=...'`。
网关 journal 里 `gateway_store` 的知识审计带 `outcome=`、`dify_trial` 带 `duration_ms=`。

**合成混合日志实测（旧命令）**：

```
1 outcome=completed      ← 本片的那条
1 outcome=refused        ← 知识审计
10 9000                  ← 本片 10ms + Dify 9000ms
```

即：1 条变成 2 条，10 ms 变成 10/9000。

**修后**（先筛事件，再统计）：同一份混合日志 ⇒ **1 条结果行 / `outcome=completed` 1 条 / 耗时 10**。

同时补了三条口径：

- 目标租户过滤只用于汇总；**「未登记租户」告警单独全租户查**，否则会被目标租户过滤吞掉，
  那个"有别的租户在用就停"的条件永远不会触发；
- 固定 `--since/--until` 观察窗口；
- **读取失败 / 零样本 / 有样本**三种状态分开写，"读不到日志"不能写成"没有行"。

## 修正三：模型调用的归因

**缺陷**：停止条件原来写"同租户同秒出现 `routing` 指标或新建 `run-` 目录 ⇒ 落回了模型路径"。
但 `agent_run_metrics` 与 `runs/` 都**没有 `qa_id`**；共享网关上的普通问答会在同一秒写出
同样的行 —— 那个口径会把别人的正常问答记成本片事故并误停用。

**改为**：

| 手上有什么 | 怎么写 |
|---|---|
| 某个 `qa_id` 在 `assistant_questions` 与指标/`runs` 两侧都对上 | 可归因，按停止条件处理 |
| 只有同租户同秒的 `routing` 行，对不上任何 `qa_id` | **待核实**，附候选行数与 `routing` 行数 |
| 本路径的 QA 行带 `run-` 前缀或 `token_count > 0` | 可归因（它自己的行），按停止条件处理 |

**不新增全链路追踪**（那是另一片的范围）。

## 事实口径更正

| 原写法 | 实际 | 覆盖 |
|---|---|---|
| `qa_id` 仅成功有值 | 证据落盘失败 503 时 QA 行已建出，`qa_id` 有值但 `outcome=failed` | `test_evidence_failure_keeps_the_real_qa_id` |
| HTTP 200 = 查到订单 | 200 clarification 的 `qa_id=-`，只要求补订单号 | `test_a_clarification_is_a_200_without_an_order` |
| 401/422 都有行 | 只有**候选形成之后**的 4xx 有行；初始校验失败没有 | 文档 |
| outcome 四类 | **只有三类** | `test_outcome_is_exactly_one_of_three_values` |

## 结果

| 项 | 结果 |
|---|---|
| 既有回归 | **56 passed**（未改断言、未加 skip） |
| 新增 | 原 18 + 新 5 = **23 passed** |
| 合计 | **79 passed** |
| 真实 HTTP | `observe_consumer_order_result.py`：**23/23 检查通过**，11 条结果行（成功 5 / 拒绝 4 / 失败 2） |
| 反向验证 | 把 401 排除出拒绝集合 ⇒ **2 条转红**；还原后 79 passed |
| 静态检查 | `ruff check` / `format --check` 干净 |

真实 HTTP 证据：`var/verify-evidence/consumer-order-observation.json`（本地运行产物，未入仓）。
其中新增的断言："a 401 is counted as refused, never as a 5xx failure" 与
"every failed row is a real 5xx (no 4xx leaked into the failure denominator)"。

11 行的构成：

- **成功 5**：常规成功 1 + 四并发 4；
- **拒绝 4**：403 缺读权限、**401 重认证失效**、404 订单不可见、404 路由前；
- **失败 2**：502 CPMS、500 未预期异常。

（普通问答与未认证请求不产生行，所以不在 11 条内。）

## 部署状态（本片不处理）

**已合并、尚未部署。** CD 因 `pyproject.toml` 摘要漂移在上传前拒绝（差异来自 #1023 的
pytest `addopts`）；`uv.lock` 两侧逐字相同，**没有依赖需要安装**。生产仍是
`0.1.0+26bc23a34fd4`，本模块与观察行在生产上均**不存在**，开关仍关闭。
本片未执行 `uv sync`、未改 41 清单、未重启或重跑生产 CD。

## 未完成（不得写成已验证）

- 端到端时延、按 `request_id` 跨层串联：仍判不了；
- 其他入口（`GET /v1/orders/{no}/charging-status`、M3 委派）无等价观察；
- 真实用户 / 真实 MySQL / 真实 CPMS 的业务验收沿用 #1015，本片未新增。
