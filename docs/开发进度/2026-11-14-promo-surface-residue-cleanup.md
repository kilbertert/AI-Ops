# 宣传面残留整体清理（ADR-0259）

日期 2026-11-14。承接 ADR-0254：那次摘「客户案例」按钮时发现 #1125 删了
路由实现却留下了整个宣传陈述面 —— 词表、替身、规格、账本、文档都把
「宣传卡」写成活路径。本次专门清掉。

- **死代码**：`PROMO_EMPTY_MESSAGES` / `PROMO_UNAVAILABLE_MESSAGES` 两张全语言
  i18n 表删除（零运行消费者）；`test_assistant_api.py` 替身的
  `start_assistant_qa` 死桩删除。17 文件 +65/-233。
- **死合同**：`acceptance.feature` 宣传隔离 Rule 变墓碑注释；`qa-plan.md`
  七处宣传行/节加失效注；八份活文档的活声明订正（含两处指向已删文件
  `promo_agents.py` 的证据引用）。
- **保留**：`metrics_store` promo 词表、`qa_rag._pinned_promo_agents`、
  intent 门禁/白名单 —— 均为活机制或读存量必需，注释订正为「历史/休眠」。
- **验证**：受影响测试 255 条全过，ruff 干净；详见
  `../validation/2026-11-14-promo-surface-residue-cleanup.md`。
- **范围外**：41 disabled 行与宣传 Agent 本体（ADR-0254 划定的审计痕迹）。
