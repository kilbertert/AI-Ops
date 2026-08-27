# PR-C 任务清单:`docs/plan-and-workflow-sync`

**目标**:把方案文档与 AFK 工作流文档同步到 D 方案(已锁定);标注"TDengine 段等 tsdata 真正补洞后回收"。**纯文档**,无代码。

**Blocked by**:None(可与 PR-A 并行)

---

## Acceptance criteria

- [ ] `docs/diag-query-api-plan.md` 修订:
  - §1 背景加一句:"本方案最终采用 D 方案(2026-08-27 锁定),TDengine 段暂留直连,Java 侧不动,tsdata 侧不动"
  - §3 范围(进/不进)调整:进 4 类查询(订单/设备/Redis/占位费);TDengine 段两个查询方法标注"暂不进,等 tsdata 补洞后追加"
  - §10 收口计划改写为"分阶段(2/3 凭据 → 1/3 凭据 → 0/3 凭据)":Phase 1/2 描述保持,Phase 3 拆为 Phase 3a(本 PR 后删 MySQL+Redis 凭据) + Phase 3b(待 tsdata 补洞后删 TDengine 凭据)
  - §12 风险表追加 D 方案风险:"TDengine 段仍直连,直到 tsdata 真正补洞之前 AI-Ops 仍持有 TDengine 凭据" + "D 方案不解决 tsdata 自身被任意人访问的问题"
- [ ] `docs/afk-workflow.md` 已修改的内容(本会话早前)合并进来:把"label `agent:to-issues` → implement"改成"ready-for-agent → dispatch-only,与 Plan A 一致"
- [ ] 新建 `docs/afk-cutover/decisions.md`:
  - 简短记录 D 方案的决策时间 / 决策人 / 决策原因 / 影响的 issue 列表
  - 指向团队记忆 mem-20260827-ranlei-005(候选期,待人工批准)
- [ ] `uv run pytest -q` 全过(无功能性变化,但要确认文档不破坏任何 doc-test)
- [ ] 增量变更 ≤ 300 行(纯文档)

---

## Out of scope(本 PR 不做)

- 任何 `src/aiops_diagnostics/` 改动
- 任何 `.env*` 改动
- 任何 `docs/architecture.md` 改动(架构层未变)

---

## 评审关注点

- D 方案明确标注"已锁定" vs "草案":不能让读者误以为还在四个方案中选
- 引用团队记忆时,标注"候选 / 未批准"状态,不要给"事实"错觉
- 风险表第 N 条(关于 TDengine 段)要写明:**当前生产环境 AI-Ops 仍持有 TDengine 凭据,直到 Phase 3b 完成**

---

## 与 PR-A/PR-B 的接缝

- 本 PR 可与 PR-A / PR-B **并行**发 PR(都基于 main)
- 三者合并顺序任意(文档 + 代码 + 模板独立可审)
