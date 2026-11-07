# 2026-11-07 运行结果可交付性收敛（#1443）验收记录

## 一、确定性检查（可复现）

全部在仓库内执行，两处：canonical checkout `/home/claude/Projects/AI-Ops`（分支 `main`、HEAD `f12af6ab`）与交付 worktree `/home/claude/Projects/.worktrees/AI-Ops-result-deliverability-single-predicate`（分支 `fix/result-deliverability-single-predicate`、提交 `8fe13024`、基于 `origin/main` `03aca048`，PR https://github.com/kilbertert/AI-Ops/pull/1444）。

| 命令 | 结果 |
| --- | --- |
| `uv run ruff check` | All checks passed |
| `uv run ruff format --check`（改动文件） | 通过（本次 format 重排 3 个文件） |
| `uv run pytest -o addopts="-n 4" -q`（worktree，交付基线） | **4730 passed / 9 skipped / 0 failed**（4739 collected，220.69s） |
| 同上（canonical checkout `f12af6ab`） | 4724 passed / 9 skipped / 0 failed（4733 collected，224.84s）；差额 6 条来自 `origin/main` 领先的 7 个提交新增的测试 |
| `uv run pytest tests/test_readme_architecture.py tests/test_repository_layout.py tests/test_runtime_retirement.py tests/test_result_deliverability_convergence.py tests/execution/test_run_deliverability.py -o addopts="" -q` | **78 passed** |
| `uv run pytest tests/execution tests/consumer tests/operations` | 1368 passed / 1 skipped |
| `uv run pytest tests/gateway tests/dify` | 全绿（EXIT=0） |

### 唯一失败与修复

全量跑第一次出现 `tests/test_readme_architecture.py::test_every_package_module_is_named_in_the_readme` 失败：新增模块 `src/aiops_diagnostics/execution/run_deliverability.py` 未写进 README 第三节分层表。修复方式是在 `README.md:78` 的「节点与试用」行 `execution.agent_runs.py` 之后补上 `execution.run_deliverability.py`，复跑 78 passed；最终全量 4730 passed / 9 skipped / 0 failed（worktree）。

### 新增的长期检查（按 `docs/agents/verification-workflow.md` 的一句话说明）

`tests/test_result_deliverability_convergence.py` 拦截的具体错误是「有人忘了判 TTL」：`src/` 内若再次出现「同一布尔表达式里手写 `state == "succeeded"` 与 `expires_at` 比较」，或「`by_qa()` 之后仅凭 `state` 决定交付」的形状，测试变红。判据来源是 #1443 的 PRD Solution d 与既有 `tests/test_transport_convergence.py` 的同形做法；既有检查（`tests/execution/test_app_runtime_dify_entry.py::test_a_successful_readback_is_refused_after_its_ttl` 与 `tests/consumer/test_consumer_chat_service.py::test_expired_result_is_not_delivered_from_qa_or_conversation`）只覆盖 legacy 一条路径，v6 两个交付面在无测试约束的情况下被漏掉——这正是本 PRD 指出的缺口。

### 回归基线（重构前后逐条不变）

`tests/execution/test_app_runtime_dify_entry.py::test_a_successful_readback_is_refused_after_its_ttl` 与 `tests/consumer/test_consumer_chat_service.py::test_expired_result_is_not_delivered_from_qa_or_conversation` 未修改，均通过。

### 行为变更的实测证据

`tests/consumer/test_consumer_agent.py`：v6 夹具下，把 `chat_dify_runs.expires_at` 改成过去时间后，`GET /v1/assistant/questions/{qa_id}` 由 **200（带正文）变为 410**，同会话下一问的 `conversation_context.history` 由 **含上一轮 answer/order_ids 变为 `[]`**。

## 二、Hack 反例

**未执行。** 按 `docs/agents/verification-workflow.md`，独立 Hacker 验收须由**另一会话**完成（指定 claude-deepseek 的 `deepseek-v4.1-flash`），本记录不代替该步骤。

待补的反例方向（供 Hacker 会话使用，均来自 PRD 指出的失效方式「忘记是静默的」）：

1. 把 `require_result()` v6 分支的 `if not deliverable(run): ...` 删掉 → `tests/consumer/test_consumer_agent.py::test_an_expired_run_is_refused_and_not_inherited` 应变红（今天为 200）。
2. 把 `consumer_agent.conversation_context()` 的 `not deliverable(run)` 换回 `run.state != "succeeded"` → 同上用例变红。
3. 在 `src/` 任意模块写回 `run.state != "succeeded" or run.expires_at <= datetime.now(UTC)` 组合 → 守护测试规则 1 变红。
4. 把 `gateway_api.py` 的 `deliverable(latest)` 换回 `not dify_run_still_valid(latest)`（重新导入已降级的 `still_valid`）→ 守护不报（`still_valid` 是允许的存活判据入口），需靠两引擎一致性用例与 code review 兜住——**这是本守护的已知盲区**。

## 三、用户路径与未验范围

- 用户路径（C 端 v6）：`POST /v1/assistant/questions` → 回读 `GET /v1/assistant/questions/{qa_id}`；过期后回读返回 410 `CHATFLOW_RESULT_EXPIRED`。管家路径：`authorize_readback` → `result()`；过期后返回 `OPERATOR_RESULT_RETRACTED`（与「授权已撤回」同码，未新增错误码）。
- **未验范围**：
  - 没有真实 Dify 上游、没有生产 `AIOPS_DATA_HOME`，所有证据来自仓库内夹具 + 真实 SQLite/uvicorn 测试入口。
  - 没有用 `verify-aiops-gateway` skill 在一次性数据根真跑 Gateway：本次改动不动路由/鉴权/注册/响应形状（`gateway_api.py` 只把同一组条件换成同一谓词，404/410 码与消息不变），因此按 `AGENTS.md` 的判据不属于「影响 Gateway 对外行为」的强制真跑范围；如评审认为需要，可在合并前补一次。
  - 没有执行任何 ssh / scp / redis-cli / systemctl 操作（本轮只跑 `uv run pytest` 与 `uv run ruff`），操作知识自检 grep 命中既有文档，无需新增手册。

## 四、业务验收

**未完成业务验收。** 本记录只包含自动化检查、夹具级回归与源码级守护证据；没有工程师确认的真实故障案例，不把夹具或测试绿灯写成真实故障结论。

## 五、复核补记（第二会话评审，2026-11-07）

由独立于实现会话的评审复核后补记；原文未改动，分歧处以本节为准。

### 复核执行的验证

- 对照 #1443 PRD a–e 逐条审计 diff：判据定义、`still_valid` 降级、全部调用点改判、404/410 合同分离，均属实。
- 现存 6 处 `still_valid` 调用点逐一核对，全部是 `state != "running"` 配对的**存活判据**（tool 回调、trial、上传中途活性检查），非交付面，保留正确。
- `src/` 剩余 `expires_at` 裸比较全部在他域（`chat_data_grant.usable`、`knowledge_retrieval` grant 清理、会话 30 天保留、JWT exp、hop ticket、`agent_runs` 内部收敛 SQL），属定义处或 PRD 范围外。
- 变异抽查（评审会话注入后已还原）：v6 分支删 `deliverable` 检查 → `test_an_expired_run_is_refused_and_not_inherited` 变红；写回「手写 state+窗口组合」与「by_qa 后仅凭 state」两种形状 → AST 守护两条规则分别变红。守护有真实拦截力。
- worktree `.venv` editable 安装指向 worktree 自身 `src`，上文测试数字可信；评审另复跑 51 条相关用例全绿。

### verify-aiops-gateway（补 §三 的缺口）

§三 称「不属对外行为变更故不需真跑」——v6 回读 200→410 **正是对外行为变更**（PRD-e 的本意），`AGENTS.md` 的判据应适用。评审已在交付 worktree 执行：

```
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/gateway
```

一次性数据根 `/tmp/aiops-verify-z116z161`、动态回环端口、真实 `aiops-gateway serve`（1.2.7）：`/health` 只读 200；`/v1/assistant/questions`、`/v1/agents` 匿名 401；`/v1/openapi.json` 200；`/v1/runs`、`/v1/enroll`、`/v1/standard/diagnoses`、`/v1/dify/tools/order_snapshot` 均 404 —— 全部通过，证据 `dist/verification/gateway/aiops-gateway.json`（gitignored）。该 skill 自身声明不覆盖远端 Dify/Java 执行，v6 语义由 §一 的夹具级用例承载。

### 更正与补充

1. **`authorize_readback`（`operator_cpms.py:434`）是新增判据，不是平移。** 基线该行只有 `still_authorized`，没有窗口比较——PRD 把 `:428-430` 误记为内联比较点。行为差异：过期的 `succeeded` 行（`by_qa` 不收敛）以前会先做 target 查找、甚至发一次真实 CPMS 请求，到 `result()` 才被拒；现在入口即拒。错误码同为 `OPERATOR_RESULT_RETRACTED`（410），但「过期 + target 缺失/scope 变更」的组合从 403 变为 410。方向与 PRD-e 同一缺陷形状，属合理补充，但「窗口类 4 处平移」的表述少算了这一处新闸门。
2. **管家侧同形状表面未收敛（PRD 未点名，记为开放问题）**：`operator_cpms.py:651`（`history_runs` 装配）与 `:945` `_previous_plan` 用 `by_qa` + `snapshot` 存在性把上一轮数据喂给下一轮，与 consumer v6 历史继承同一形状，无窗口判据，且因不比较 `state == "succeeded"` 对 AST 守护不可见。管家会话跨 180 秒继承上一轮证据是否有意，留 #1443 由 PRD 作者裁定；本 PR 未改其行为。
3. **守护残余盲区**（除 §二.4 已声明者外）：`"succeeded" == run.state` 反向写法与 `run.state in ("succeeded",)` 成员写法可绕过两条规则——威胁模型是「忘记判」而非对抗性规避，不视为缺陷，记录在案。
4. §一 的 4730/4724 差额归因方向写反：6 条是**本 PR 新增**用例（3 谓词 + 1 v6 + 2 守护），非「origin/main 领先的 7 个提交」；且 `origin/main` 即本分支基线 `03aca048`，并未领先。数字本身一致。
