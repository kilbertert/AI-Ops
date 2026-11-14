# 「客户案例」快捷指令整体退役（consumer + operator，ADR-0254）

日期 2026-11-14，PR #1510。



- **需求**：「客户案例」这条快捷指令不再要，两个入口全关。只摘按钮 ——
  `case_exploration` 意图保留，自由文本带案例线索仍走宣传面（ADR-0031 划过的边界沿用）。
- **代码**：`_BUNDLED_SHORTCUTS` 的 `consumer`/`operator` 两块与 `_SHARED_ACTION_FIELDS`
  中的 `case_exploration` 定义删除。新种子集：consumer = 智能检测 / 故障上报 / 电池报告横幅；
  operator = 订单检测 / 运维巡检。受影响断言按新集合订正（11 处测试断言）。
- **数据（41 已执行）**：备份 `/var/backups/aiops-41/shortcuts-retire-20261114-104704/` 后
  `disable` 全部 5 行（2 平台 + 3 租户，含 operator 侧绑 `agt_ed443cae…#v3` 的行）。
  `list_effective` 两租户 × 两入口断言不含该 code；真实会话 HTTP 面标未验（自签身份
  过不了身份类别闸，见 `../validation/2026-11-14-case-exploration-retired.md`）。前端硬编码按钮的既有偏差仍在。
- **验证**：pytest 5021 passed / 0 failed；ruff 干净。决定与替代形态见 ADR-0254。

