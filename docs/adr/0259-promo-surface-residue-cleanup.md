# 宣传面残留整体清理（#1125 退役面的收尾）

日期：2026-11-14
状态：已采纳
影响版本：`common/i18n.py`、`acceptance.feature`、`qa-plan.md`、
`docs/agents/*`、`docs/architecture/repository-layout.md`、`docs/testing-api-guide.md`、
`tests/common/test_i18n*.py`、`tests/gateway/test_assistant_api.py`

## 背景

#1125 的 Python harness 退役把宣传路由 `_promo_route` / `_start_promo_qa` /
`promo_agents.py` 一并删掉了，但删的是**路由实现**，不是**宣传面**：词表、
测试替身、规格合同、验收账本与一堆活文档里仍然把「宣传卡」写成一条活着的
路径。ADR-0254 摘「客户案例」按钮时顺手核实过：41 上全部宣传意图行早已
`disabled`，自由文本里的案例线索与滞留 `shortcut_code` 均按普通问句处理 ——
宣传面在运行时已经死透，残留物只是在持续生产误导。

## 决定

**把宣传面残留分三类处理：死代码删、死合同标记失效、活机制注释订正。**

1. **删除（无消费者）**：`i18n.py` 的 `PROMO_EMPTY_MESSAGES` 与
   `PROMO_UNAVAILABLE_MESSAGES` 两张全语言表（唯一消费者是 i18n 结构测试自身）；
   `test_assistant_api.py` 里替身的 `start_assistant_qa` 死桩（src 侧同名方法
   已不存在，无任何调用方）及随之死去的 `qa_calls`/`skip_retrieval` 属性。
2. **标记失效（历史保留）**：`acceptance.feature` 的「宣传内容与客服 FAQ 隔离」
   Rule 整段与其余案例场景改为墓碑注释/改写；`qa-plan.md` 中指向宣传路径的
   PASS/PARTIAL/BLOCKED/待办各行加 2026-11-14 失效注（账本只标不删）；
   `operator-authorization-questions.md`、`operator-repair-blueprint.md`、
   `frontend-api-brief.md` 等活文档里的宣传活声明加同日订正。
3. **保留并订正注释（活机制/历史词表）**：
   - `metrics_store` 的 `"promo"` 路由类型 —— `record()` 对未声明类型拒绝，
     存量行还要靠它读；注释标为历史词表。
   - `qa_rag._pinned_promo_agents` —— 管理 API 仍接受宣传意图行，钉住的
     Agent 被客服路由选中仍是错答案；排除保留，docstring 说明 pin 已是
     休眠元数据。
   - `SHORTCUT_INTENTS` 门禁与 `target_agent_version` 白名单
     （`{case_exploration, solution_discovery}`）—— ADR-0254 已定过
     「生命周期能力不退役」，照旧。

## 为什么不连存量数据一起清

41 的 disabled 宣传行与 `agt_ed443cae…` 等宣传 Agent 本体是 ADR-0254 明确
划出范围的审计痕迹；本 ADR 只管「陈述与事实不符」的腐烂面，不碰数据。

## 后果

- i18n 表从 5 张用户面表收敛为 3 张；11 语结构覆盖门禁不再守护死文案。
- 规格文本不再声称存在宣传卡合同；QA 账本里的历史 PASS 仍可读但明确失效。
- 下一个把「客户案例」加回来的人不会再被 `promo_agents.py:80-108` 这类
  指向已删文件的证据引用骗到。
