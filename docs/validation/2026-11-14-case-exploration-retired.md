# 「客户案例」快捷指令整体退役：种子摘除 + 41 数据行停用（ADR-0254）

- 日期：2026-11-14
- PR：#1510



**决定**：产品退役 `case_exploration` 这条快捷指令，`consumer` 与 `operator`
两个入口全摘。只摘按钮 —— 意图与文案保留作门禁/存量行元数据。

> **订正（同日独立复核发现）**：初稿沿用 ADR-0031 的「自由文本仍走宣传面」
> 表述是错的 —— 宣传路由（`_promo_route`/`_start_promo_qa`/`promo_agents.py`）
> 已随 #1125 的 Python harness 退役一并删除，`PROMO_*` i18n 表自那时起无运行
> 消费者（仅 i18n 语言覆盖测试引用）。带案例线索的自由文本与滞留 code 一样，
> 按普通问句走既有问答路径。下文相关表述已订正。

**代码侧**：`_BUNDLED_SHORTCUTS` 两个入口块与 `_SHARED_ACTION_FIELDS` 里的
`case_exploration` 定义已删；`SHORTCUT_INTENTS` / `STABLE_CODES` / i18n 文案保留
（自由文本路径仍用）。受影响测试按新动作集订正：operator = smart_diagnosis +
operations_review，consumer = smart_diagnosis + report_fault（banner 另算）。

**数据侧（41，当日执行）**：WAL 安全备份先于写 —
`/var/backups/aiops-41/shortcuts-retire-20261114-104704/gateway.db`。
按 runbook §3.1-sexies「全量停用」形态 `disable` 全部 5 行（不删行，保留审计痕迹）：

| 行 | 停用前 rev → 后 |
|---|---|
| `__platform__/consumer` | 8 → 9 |
| `__platform__/operator` | 5 → 6 |
| `1899282205965029376/consumer` | 8 → 9 |
| `1942105476598861824/consumer` | 11 → 12 |
| `1942105476598861824/operator`（绑 `agt_ed443cae…#v3`） | 2 → 3 |

停用后 `store.list_effective` 逐租户逐入口断言：`case_exploration` 不出现在
两个租户 × 两个入口的有效列表（consumer = [smart_diagnosis, report_fault]，
operator = [smart_diagnosis, operations_review]）。

**证据边界（据实）**：

- 入口级 HTTP 验收用自签身份不可行：consumer 入口对 type=1 身份判
  `403 PLATFORM_FORBIDDEN`（身份类别闸，与身份模型一致），operator 入口对自签身份
  `503 PLATFORM_UNAVAILABLE`（该路径要公司令牌链）。真实会话的入口复验标**未验**；
  所验的 `store.list_effective` 正是 `/v1/shortcuts` 端点调用的同一个归并缝
  （`gateway_api.py` → `shortcut_manager.list_effective` → `store.list_effective`），
  端点是其上的薄投影。
- 前端**硬编码按钮**的既有偏差仍在（`frontend-operator-handoff.md` §6）：界面可能
  仍渲染这颗按钮，点下去落进普通问答 —— 不是停用失败，在前端改造里收。
- 停用后显式提交 `shortcut_code=case_exploration` 按「无此有效动作」处理：
  `find_effective_by_code` 经 `list_effective` 归并查不到行（disabled 不生效）
  → `shortcut=None` → 跳过快捷守卫 → 按普通问句进既有问答路径，不返回 404、
  不产出宣传卡（代码路径复核：gateway_api.py shortcut_code 分发块）。
- **独立复核（DeepSeek，diff 内联 + 全工具禁用）**：A 组确认两项文档缺陷已修
  （runbook 全量停用块 `list_all` 按租户过滤取不到租户行 → 改为 sqlite 直查枚举；
  frontend-operator-handoff §3.2 示例仍发退役 code → 改为自由文本示例）；
  B1 升级为确认项 —— 宣传路由已死的连带表述订正（本节上段、ADR-0254、
  acceptance.feature 不可用卡场景、operator-frontend-chain §4.2、
  testing-api-reference §3.3、frontend-api-brief 路由说明）。
  否掉四项：种子集合实值与判据逐字相等（B2）、migration 工具不按 intent 分支
  （B4）、存量行夹具正是「已发行继续服务」语义的正确覆盖（B5）、ADR-0254 在
  分支树中（B7）。
- **已知残留腐烂（不在本 PR 收）**：`qa-plan.md` ≥8 条验收记录仍以
  `case_exploration` 为现行前置/预期（INTENT-08、SHORTCUT-PROMO-*、
  OP-ACCEPT-01 等），`acceptance.feature` 宣传段（Rule「宣传内容与客服 FAQ
  隔离」起）整套场景描述的是 #1125 前的宣传路由合同 —— 属宣传面整体
  清理的工作，随宣传面残留（`PROMO_*` i18n 死表、`_pinned_promo_agents`
  的 starters 排除语义、`_dify_starters` 链）另起事项收。

**验证**：pytest 全量 **5021 passed / 9 skipped / 0 failed**（uv venv，-n 4）；
`ruff check` + `ruff format --check` 干净。

