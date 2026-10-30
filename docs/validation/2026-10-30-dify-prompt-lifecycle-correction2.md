# 提示词生命周期：第二轮复核修复（贯穿链、事实片段、迁移幂等）

日期：2026-10-30；复核基线 `e4059c7f`；复核原件在私有目录
`~/.cache/aiops-prompt-lifecycle-review-1223/r2/`（`review.md`、两个 probe 与结果 JSON）。

## 一、两组独立探针：红 → 绿

| 探针 | 修复前 | 修复后 |
|---|---|---|
| `r2/probe.py`（7 项） | **6 failed** | **0 failed**（`french_status=充电结束`，多语言走现有 `consumer_plain._status_name`） |
| `r2/lifecycle_probe.py`（2 项，真实发布/QA 公共 API） | **2 failed**（410 / failed） | **0 failed**（两例 200 completed，`dify_publishes=2`） |

## 二、贯穿链（本轮第一优先，已完成）

`发布A → QA → 只改提示词发布B → 旧 QA 仍可读 → 撤权拒绝`，走真实 `/v1/dify/publish` 与 QA 入口：

- `chat_execution.binding_for_version()`：用**不可变版本快照**重建签发时的绑定（Dify 每次发布
  生成新 `workflow_id`，旧 run 的绑定必须按快照重建，不能拿当前行/当前 `selected` 冒充）。
- `consumer_chat_service.deliverable_binding()`：现行权限 → 路由 active → 冻结绑定指纹必须复现
  `run.binding_digest`；任一不成立即 410。换凭据、内容被换、撤回、版本缺失都拒。
- `require_pinned()` 取代 `execute` 里 5 处 `current()`：在途 run 用签发时的绑定跑完；
  允许的唯一差别是"路由推进了一版"，凭据/app/未推进的变化仍拒。
- 撤权/撤回/过期/取消：`_route_active` + `still_valid` + 取消信号，行为未放宽。

## 三、事实输出（改为整句片段）

`consumer_facts.facts_for()` 现在产出**带含义的整句**：`order_line`、`billing_line`
（`账单合计 27.36 元，系统记录的支付金额 20 元，退款 0 元`）、`status_line`、`observed_line`。
裸值片段（`total_amount`/`paid_amount`/`status`）**不再是可写片段**，因此
`每小时{{fact:paid_amount}}元`、`合计{{fact:total_amount}}美元`、`这单并非{{fact:status}}` 全部被拒。
一旦使用片段，片段之外不许出现数字（混写即拒）。legacy 契约恢复"金额必须都在"的完整性校验。
找站说明补封闭词表（不收钱/可以充/能充…）与站名形状判定。

## 四、迁移与协议装配

`--live-documents` 按名字去重 `answer_fact_contract`（第二次迁移不再产生重复环境变量，
`publication_settings` 可再次通过）；运营 SYSTEM **不再 strip**；旧协议块（任意代
`<!-- aiops-protocol:`）被识别并替换，不叠加。

## 五、实跑（最终 head）

| 项 | 结果 |
|---|---|
| `uv run pytest -q tests/ -n 4` | **3683 passed, 9 skipped, 0 failed** |
| `uv run ruff check` / `format --check` | 通过 |
| `tools/verification/verify_prompt_lifecycle.py` | 24 项 0 失败（页面/模型项仍 NOT_RUN） |

## 六、本轮未完成（如实记录）

1. **管家端旧 run 的 `route_revision` 等值判据未改**：`operator_cpms` 四处仍按等值判，
   一次正常发布会让管家在途 run 失败。改动会连带 40 条既有用例（它们用"推进 revision"模拟撤回），
   需要把它们改成真实 `store.retract()` 才能安全落地；本轮预算内未做，**不宣称完成**。
2. **隔离页面/真实模型验收未执行**：未尝试启动 `deploy/dify-chat/docker-compose.acceptance.yaml`
   与 `tools/verification/verify_consumer_chatflow.py`，因此页面级（真实编辑文本框 + 预览/发布/
   恢复）与真实模型证据仍为 NOT_RUN。
3. **替代交付分支未建立**：`Workflow policy` 仍因已发布提交 `12d16b79` 缺 ADR 而红；
   修复需按 `dev-worktree` 从最新 main 建替代分支承接净 diff（含 ADR 同提交）并开替代 PR。
4. 16 格仍是合成文本；`qa-plan.md` 的参数未更新。
