# 宣传面残留整体清理（#1125 退役面收尾，ADR-0259）

- 日期：2026-11-14
- 范围：死代码删除 + 死合同标记失效 + 活机制注释订正；**不碰 41 数据**

**背景事实（独立复核，非设计文档推断）**：#1125 删的是路由实现
（`_promo_route` / `_start_promo_qa` / `promo_agents.py`），残留物此后持续
把「宣传卡」写成一条活路径。41 实测全部宣传意图行早已 `disabled`（ADR-0254
已核），自由文本案例线索与滞留 `shortcut_code` 均按普通问句处理 —— 宣传面
运行时死透，本次清的是陈述面。

## 三类处置

1. **删除**：`i18n.py` 的 `PROMO_EMPTY_MESSAGES` / `PROMO_UNAVAILABLE_MESSAGES`
   （唯一消费者是 i18n 结构测试自身，已同步摘除）；`test_assistant_api.py`
   替身的 `start_assistant_qa` 死桩与 `qa_calls`/`skip_retrieval` 属性
   （src 侧无此方法、无调用方；`runtime._qa` 字典保留 —— 三条用例断言
   `_qa == {}` 是「QA 路由不得建作业」的活守卫）。
2. **标记失效**：`acceptance.feature`「宣传内容与客服 FAQ 隔离」Rule 整段 →
   墓碑注释；其余 case_exploration 场景改写为在役 code。`qa-plan.md` 七处
   宣传相关行/节加 2026-11-14 失效注（账本标不删）。活文档订正：
   `operator-authorization-questions.md`（头注 + C10/P1-6 失效）、
   `operator-repair-blueprint.md`（#413 两处待验项失效）、
   `frontend-api-brief.md`（result 形状说明、`target_agent_version` 字段）、
   `frontend-operator-handoff.md`（§5 清单、§6 边界）、
   `assistant-cancel-handoff.md`、`testing-api-guide.md`、
   `repository-layout.md`、`env-41-runbook.md`（两处配方示例注）。
3. **保留并订正注释**：`metrics_store` `"promo"` 路由类型（读存量行必需，
   `record()` 拒未声明类型）；`qa_rag._pinned_promo_agents`（管理 API 仍收
   宣传意图行，pin 过的 Agent 被客服路由选中仍是错答案）；
   `SHORTCUT_INTENTS` 门禁与 `target_agent_version` 白名单（ADR-0254 已定
   「生命周期能力不退役」）。

## 验证

- `uv run pytest` 受影响面 255 条全过：`tests/common/test_i18n*.py`、
  `tests/gateway/test_assistant_api.py`、`tests/publication/test_qa_rag.py`、
  `tests/gateway/test_agent_metrics.py`、`tests/test_runtime_retirement.py`、
  `tests/test_repository_layout.py`、`tests/consumer/test_consumer_copy*.py`。
- `uv run ruff format/check` 干净。
- 残留扫描：全仓 `宣传|promo|case_exploration|solution_discovery` 复查，
  剩余命中均为「已标失效的历史记录」「真实 KB 素材名」「无关语义
  （promotion workflow / 宣传页面）」三类 —— 无未标记的活声明。

## 边界

- 41 数据行与 `agt_ed443cae…` 宣传 Agent 本体不动（ADR-0254 划出的范围）。
- 宣传面「回来」的路径仍在：管理 API 接受宣传意图行 + pin 排除仍在 ——
  能重建是数据，不是产品承诺。
