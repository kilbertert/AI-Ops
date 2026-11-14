# 宣传面残留整体清理（ADR-0259）

日期 2026-11-14。承接 ADR-0254：那次摘「客户案例」按钮时发现 #1125 删了
路由实现却留下了整个宣传陈述面 —— 词表、替身、规格、账本、文档都把
「宣传卡」写成活路径。本次专门清掉。

- **死代码**：`PROMO_EMPTY_MESSAGES` / `PROMO_UNAVAILABLE_MESSAGES` 两张全语言
  i18n 表删除（消费者 = 两个 i18n 结构测试 + `derive_zh_hant` 登记表，同步摘除）；
  五个测试替身的 `start_assistant_qa` 死桩删除（src 侧方法已不存在）。
- **死合同**：`acceptance.feature` 宣传隔离 Rule 变墓碑注释；`qa-plan.md`
  七处宣传行/节加失效注；八份活文档的活声明订正（含两处指向已删文件
  `promo_agents.py` 的证据引用）。
- **保留**：`metrics_store` promo 词表、`qa_rag._pinned_promo_agents`、
  intent 门禁/白名单 —— 均为活机制或读存量必需，注释订正为「历史/休眠」。
- **独立复核（DeepSeek）**：判 MERGE-WITH-FIXES —— A1 抓出 `_qa == {}` 断言
  在删 stub 后恒真（本 PR 要消的那类「假守卫」出现在了自己的测试里），
  已删断言并改写守卫表述；B 级 6 条全是「记述与 diff 不一致」，逐一修掉
  （影响面漏列、表数错、「同日」错并 #1125 与 ADR-0254、`unavailable` 语义
  误删、两处未标记残留、漏记三处场景删除）。
- **验证**：受影响测试分批全过，ruff 干净；详见
  `../validation/2026-11-14-promo-surface-residue-cleanup.md`。
- **范围外**：41 disabled 行与宣传 Agent 本体（ADR-0254 划定的审计痕迹）。
