# llmdoc 同步验证：e5e952e..30bcfcbf（2026-11-12）

分支 `docs/llmdoc-sync-post-runlog`，提交 `ff3a355`（docs）+ `3b43d92`（meta），PR #1489。

## 方法

`/llmdoc:update` deep 模式（delta 自行建议：17 篇受影响 + 97 个未映射改动）。
六个 investigator 按子系统簇各自把目标文档逐条判言与源码核对，证据写入
`.llmdoc-tmp/investigations/`（仓库临时区，不提交）；六个 recorder 经
stable-knowledge gate 只写复核为真的修正。

## 实际运行的检查

- `npx @tokenroll/llmdoc status/delta`：基线 e5e952e 落后 22 提交、17 篇 impacted。
- `npx @tokenroll/llmdoc validate`：**ok**（提交前在 worktree 复跑亦 ok）。
- `llmdoc commit`：`ff3a355` + meta `3b43d92`，16 篇指纹刷新。
- 每条写入判言由 recorder 对照源码复核（investigator 报告只作证据不作事实）。

## 结论

16 篇文档完成同步（改动明细见 PR 描述）：ADR-0244 schema 单一权威、ADR-0248
证据交付、ADR-0249 运行日志明细、消费端原值系列，及部署/CD/xdist/i18n 等
既有判言的更正。3 篇非 impacted 文档（analytics、governance-afk、knowledge）
未动。已判言为真的旧内容逐条保留。

## 未验边界

- investigator/recorder 的「逐条复核」是模型核查而非确定性测试；关键新判言
  （schema ensure、evidence_only、投影白名单）均有对应源码行与 ADR/测试佐证，
  但 llmdoc 层没有可执行的文档-代码一致性断言。
- `.llmdoc-tmp/investigations/` 六份证据报告按约定不提交，随用随弃。
