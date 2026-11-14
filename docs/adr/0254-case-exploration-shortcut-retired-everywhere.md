# 「客户案例」快捷指令从两个入口整体退役

日期：2026-11-14
状态：已采纳（**部分反转 ADR-0050** —— 本次范围更大：不只 `operator`，`consumer` 一起摘）
影响版本：`shortcut_lifecycle._BUNDLED_SHORTCUTS`（`consumer` 与 `operator` 两块）、
`_SHARED_ACTION_FIELDS`、`acceptance.feature`、41 数据行（按 runbook §3.1-sexies 停用）

## 背景

`case_exploration`（「客户案例」）的产品史是来回的：

- **#427** 起挂在 `consumer` 与 `operator` 两个入口的种子动作里；
- **2026-10-16**（#803 / ADR-0031）从 `operator` 摘除，理由是「平台行绑不了
  `target_agent_version`，该入口产不出这张卡」；
- **2026-10-19**（ADR-0050）恢复：机制判断不变，结论是「平台行给可见性 +
  租户行给绑定」本就是这个表设计的常规形状；
- **2026-11-14** 产品口径更新：**这条指令不再要了，两个入口全关。**

## 决定

**把 `case_exploration` 从 `_BUNDLED_SHORTCUTS` 的 `consumer` 与 `operator`
两块种子以及 `_SHARED_ACTION_FIELDS` 中一并删除。**

摘掉的只有**按钮**。「只摘按钮」是 ADR-0031 时用户明确划过的边界、本次沿用：

- `case_exploration` 仍在 `SHORTCUT_INTENTS` 与 `STABLE_CODES` 中 —— 自由文本
  带案例线索时分类器仍可判出该意图、走宣传面；没有租户行绑宣传智能体时照旧
  返回那句诚实的「客户案例服务暂时不可用」。自由文本不是快捷指令，不在本次范围。
- 管理 API（创建/发布 `case_exploration` 行）不封堵 —— 生命周期能力本身不退役，
  谁也不能排除这条指令日后以某个受控形态回来；能重建是数据，不是产品承诺。

同 ADR-0031 记过的那条：**种子决定将来，数据行决定现在**。本次同样两条都做：

- 代码：删种子（本次交付）；
- 数据：41 上 `consumer`/`operator` 的平台行与各租户行全部 `disable`
  （运维动作，按 `env-41-runbook.md` §3.1-sexies，含验收命令）。

## 被否掉的替代形态

- **(a) 只摘 `operator`，`consumer` 保留。** 否掉：用户口径是「这一个指令不要了」
  —— 指令本身退役，不是某个入口收窄。
- **(b) 顺手把 `case_exploration` 意图也删了。** 否掉（沿用 ADR-0031 (b) 的结论）：
  意图是分类器从自由文本产出的东西，删掉等于让一句「我想看看客户案例」掉到
  普通问答或客服 FAQ —— 比「诚实的不可用」更糟，也超出「摘按钮」的范围。
- **(c) 只改种子不停用数据行。** 否掉：`seed_bundled` 对已存在行是 no-op，
  已 `published` 的行照旧返回 —— 「代码里没了」≠「入口没了」。
- **(d) 物理删除数据行。** 否掉：保留 `disabled` 行才有审计痕迹，且
  `list_effective` 里 disabled 行正是抑制平台行的机制。

## 后果

- **代码侧**：`consumer` 种子动作集为 `smart_diagnosis` / `report_fault` /
  `battery_report`（banner）；`operator` 为 `smart_diagnosis` / `operations_review`。
  新播种的租户不再有「客户案例」按钮。
- **数据侧（不随合并部署发生）**：41 上既有行仍是 `published`，须按
  `env-41-runbook.md` §3.1-sexies 逐行 `disable`（平台行 ×2 + 租户覆盖行，
  含 `operator` 侧绑了 `agt_ed443cae…#v3` 的那条）；停用后入口不再返回，
  `shortcut_code=case_exploration` 显式提交按「无此有效动作」处理。
- **前端仍会照旧渲染**：管家端与客端前端都可能硬编码/缓存这颗按钮
  （`frontend-operator-handoff.md` §6 的既有偏差）。停用数据行后界面可能
  仍显示它，点下去落进普通问答 —— 不是停用失败，在前端改造里一起收。
- **宣传 Agent 绑定关系**：租户行停用后 `agt_ed443cae…` 等版本不再被任何
  快捷指令引用；智能体本体不动，删除与否是另一件事。
- **ADR-0050 的「两层数据」配方成为历史**：`env-41-runbook.md` §3.1-septies
  记的那次恢复实例已反转；方法本身（定点建行）仍可用，但对象是别的动作。
