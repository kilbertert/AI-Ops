# 2026-11-12：llmdoc 同步 —— schema 权威/证据交付/运行日志窗口

分支 `docs/llmdoc-sync-post-runlog`，基线 `30bcfcbf`，PR #1489。

- 触发：`/llmdoc:update`，baseline e5e952e 落后 22 提交（llmdoc 自举之后
  的 schema 权威、消费端原值、本地化语言日志、证据交付、运行日志明细
  全部未入库）。
- 流程：deep 模式，6 investigator + 6 recorder 按簇并行；
  证据报告在 `.llmdoc-tmp/investigations/`。
- 结果：16/19 篇文档更新（详见 PR 描述与验证记录），`validate` ok，
  指纹随 meta 提交刷新。
- 已知缺口：文档判言的逐条复核是模型核查，llmdoc 层没有可执行的
  文档-代码一致性断言——`validate` 通过只证明结构与路由合法，
  不证明判言与代码逐字一致；关键判言的业务正确性仍以 ADR 与
  确定性测试套件为准。**未完成业务验收。**
- 下一步：Devin 复核闭环后合入；文档-代码一致性断言若要确定性化，
  属另行立项的治理工作，不在本轮范围。
