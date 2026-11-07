# 2026-11-07 运行结果可交付性收敛（#1443）

## 目标

把「这条运行的结果此刻还能不能交付」收敛成单一权威判据。改之前同一个问题有四种机制在回答：

1. 180 秒 TTL 常量（`agent_runs.RUN_TTL_SECONDS` / `dify_run_store.DIFY_RUN_TTL_SECONDS`）；
2. `expired` 终态（`agent_runs.get()` 会把 `succeeded` 也改写成 `expired`，`chat_dify_runs.get()` 只改 `running`）；
3. `still_valid()` 谓词（`dify_run_store.py:89`）；
4. 调用点直接比较 `state == "succeeded"`。

两条引擎对同一段窗口给出相反答案：`agent_runs.get()` 收敛集合含 `succeeded`，dify 的 `get()` 不含，只能靠第二谓词补。v6 生产路线（`callback_version=6`）的两个交付面——`require_result()` 的 v6 分支与 `consumer_agent.conversation_context()`——只判行状态，完全不看 TTL，于是过期后仍交付正文与历史。

## 已完成

### 1. 唯一判据：`execution/run_deliverability.py`（新增）

与 `publication/agent_serviceability.py` 同形的纯函数模块（Protocol + 两个函数，无类层次、无运行时状态）：

- `expired(record, *, now=None) -> bool` —— 全仓库唯一的 `expires_at` 比较（`record.expires_at <= (now or now_utc())`）。
- `deliverable(run, *, now=None) -> bool` —— 判据只有一条：`run.state == "succeeded"` 且 `not expired(run)`。

放在 `execution/` 的原因：`dify/` 已经反向依赖 `execution/`（`dify/self_service.py:39-40`、`dify/self_service_config.py:55`），谓词放这里不产生新环；该模块只 import `common.scalar_rules`，不 import `agent_runs`/`dify`，因此不会把 `gateway_config`/`identity` 拖进依赖链。

### 2. `still_valid()` 降级为内部实现

`dify_run_store.py:88-102`：函数体改为 `return not expired(run, now=now)`，docstring 重写，明说「本函数是『这一轮还在不在』这类存活判据的入口，交付判据是 `deliverable()`，不要用它」。

### 3. 12 处调用点分流

**交付类 → `deliverable()`**（7 处）：

| 位置 | 原写法 | 现写法 |
| --- | --- | --- |
| `consumer_chat_service.py:103` | `still_valid(previous)` | `deliverable(previous)` |
| `consumer_chat_service.py:397`（v6 分支，新增） | 无 | `if not deliverable(run): raise ChatRunUnavailable("CHATFLOW_RESULT_EXPIRED", ..., http_status=410)` |
| `consumer_chat_service.py:413` | `latest is None or latest.state != "succeeded" or not still_valid(latest)` | `latest is None or not deliverable(latest)` |
| `consumer_chat_service.py:497` | `not run or run.state != "succeeded" or not still_valid(run) or ...` | `not run or not deliverable(run) or ...` |
| `consumer_agent.py:56` | `run is None or run.state != "succeeded" or qa["status"] != "completed"` | `run is None or not deliverable(run) or qa["status"] != "completed"` |
| `gateway_api.py:6771-6772` | `latest.state != "succeeded" or not dify_run_still_valid(latest)` | `latest is None or not deliverable(latest)` |
| `gateway_api.py:6789` | `latest is None or latest.state != "succeeded" or not dify_run_still_valid(latest)` | `latest is None or not deliverable(latest)` |
| `gateway_api.py:6918` | `not dify_run_still_valid(latest)` | `not deliverable(latest)` |
| `operator_cpms.py:1683` | `run.state != "succeeded" or ... or run.expires_at <= now` | `not deliverable(run) or ...` |

**窗口类 → `expired()`**（6 处）：`consumer_tool_grants.py:80`（`_prune`，grant 对象）、`:122`（`_run`）、`:231`（`_current`）；`operator_cpms.py:705`（`_prune`）、`:833`（`_authorize_execution`）、`:1669`（`result()` 的 RETRACTED 分支）。

**保留 `still_valid()` 的存活判据**（6 处，行为不变）：`consumer_chat_service.py:243/:294/:305`、`consumer_trial.py:243`、`consumer_knowledge.py:587`、`consumer_agent.py:140`。

### 4. v6 两个交付面的修复（本 PRD 唯一改变现役行为的部分）

- `require_result()` v6 分支：在证据校验**之前**补 `not deliverable(run)` → 410 `CHATFLOW_RESULT_EXPIRED`（复用旧分支的码，未新造）。原 `run.state != "succeeded"` 判据被 `deliverable()` 覆盖，删去冗余。
- `consumer_agent.conversation_context()`：把「仅凭行状态」改成 `deliverable(run)`，过期的那轮不再进历史。

### 5. 新增源码级守护与用例

- `tests/test_result_deliverability_convergence.py`（新增根层测试，已同步 `tests/test_repository_layout.py` 的 `LAYOUT["tests"]` 白名单）：
  - 规则 1：任一 `ast.BoolOp` 内同时出现 `.state` 与 `"succeeded"` 比较和 `.expires_at` 属性 → 红。
  - 规则 2：函数作用域内调用 `.by_qa(` 且比较 `.state == "succeeded"`，却既未引用 `deliverable`/`expired`/`still_valid` 也未出现 `.expires_at` → 红。**必须不下降进嵌套函数作用域**，否则 `gateway_api.py` 的 `create_gateway_app` 会因嵌套函数泄漏被误报。
- `tests/execution/test_run_deliverability.py`：两引擎一致性——同一时刻、同一 TTL 对 `AgentRun` 与 `DifyRun` 必须给出同一个「能不能交付」答案；矩阵覆盖 succeeded/running/failed/expired × 窗口内/窗口边界/窗口外；另断言 `expired()` 对两种记录同判、`still_valid(dify) == not expired(dify)`、`expires_at == now` 视为已过期（与 `agent_runs._expire_in` 的 `<=` 一致）。
- `tests/consumer/test_consumer_agent.py`：新增 `test_an_expired_run_is_refused_and_not_inherited`（v6 夹具，断言超时 GET 410 且同会话新问题不继承那轮历史）；原 `test_agent_combines_tools_and_keeps_actual_answer_after_grant_expiry` 中「过期后历史仍被交付」的两条断言改为 `assert observations[1]["history"] == []`，尾部三条断言按新行为更新。

### 6. 未动的部分（按 PRD 边界）

节点引擎 `complete()`/`complete_loop()`（ADR-0057）、`chat_execution.py` 执行链、sweep/claim（ADR-0091）、`CONVERSATION_RETENTION_DAYS`、数据凭据/媒体凭据/JWT/作业表各自的 `expires_at`、Java 节点 `MAX_AGE_MILLIS`（15 分钟，故意大于 180 秒）。180 秒数值未改。

## 当前分支与提交

- 分支：`main`（HEAD `f12af6ab`），**尚未提交**。
- 改动文件：`src/aiops_diagnostics/execution/run_deliverability.py`（新增）、`src/aiops_diagnostics/dify/dify_run_store.py`、`src/aiops_diagnostics/consumer/consumer_chat_service.py`、`src/aiops_diagnostics/consumer/consumer_agent.py`、`src/aiops_diagnostics/consumer/consumer_tool_grants.py`、`src/aiops_diagnostics/operator/operator_cpms.py`、`src/aiops_diagnostics/gateway/gateway_api.py`、`README.md`；`tests/test_result_deliverability_convergence.py`（新增）、`tests/execution/test_run_deliverability.py`（新增）、`tests/test_repository_layout.py`、`tests/consumer/test_consumer_agent.py`。

## 验证结果

- `uv run ruff check` / `uv run ruff format`：通过（format 重排 3 个文件，check 修 1 处 import 顺序）。
- `uv run pytest -o addopts="-n 4" -q`：**4724 passed / 9 skipped / 0 failed**（4733 collected，224.84s）。第一次跑时唯一失败是 README 分层表未列新模块，已修。
- `uv run pytest tests/execution tests/consumer tests/operations`：1368 passed / 1 skipped。
- `uv run pytest tests/gateway tests/dify`：全绿。

## 已知缺口与下一步

1. **未做独立 Hacker 验收**：按 `docs/agents/verification-workflow.md`，需由另一会话（claude-deepseek / deepseek-v4.1-flash）独立构造反例并复现，本里程碑不代替。
2. **未在真实 `AIOPS_DATA_HOME` 上做端到端验收**：所有证据都来自仓库内夹具与真实 SQLite/uvicorn 测试入口，没有真实 Dify 上游与生产数据。没有工程师确认的真实故障结论，**未完成业务验收**。
3. **Gateway 对外行为未变**：路由/鉴权/注册/响应形状未动；`gateway_api.py` 只是把同一组条件（成功态 + 窗口）换成同一个谓词，404/410 码与消息不变。v6 分支新增的 `latest is None` 防护把理论上的 `AttributeError`(500) 变成 410，与相邻 `TRIAL_RESULT_UNAVAILABLE` 站点一致。
4. **行为变更的已知后果**：v6 会话里过期那轮不再进历史，因此该轮的订单不再出现在后续轮次的 `conversation_context()` 里；订单可见性（`_require_chat_order_visible`）是另一条机制，本 PRD 未改。这一点已在 `tests/consumer/test_consumer_agent.py` 的尾部断言中如实反映。
