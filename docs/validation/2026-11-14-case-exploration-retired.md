# 「客户案例」快捷指令整体退役：种子摘除 + 41 数据行停用（ADR-0254）

- 日期：2026-11-14
- PR：#1510



**决定**：产品退役 `case_exploration` 这条快捷指令，`consumer` 与 `operator`
两个入口全摘。只摘按钮 —— 自由文本命中该意图仍走宣传面（沿用 ADR-0031 的边界）。

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
- 停用后显式提交 `shortcut_code=case_exploration` 按「无此有效动作」处理；
  自由文本「我想看看客户案例」仍路由宣传面，无绑定时给诚实的「暂时不可用」。

**验证**：pytest 全量 **5021 passed / 9 skipped / 0 failed**（uv venv，-n 4）；
`ruff check` + `ruff format --check` 干净。

