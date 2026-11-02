# 站点工具整链：红→绿证据

日期：2026-11-02。基线 `2a59a18e`，分支 `fix/callback-site-route`。

## 复核反例（红）

| 反例 | 修复前 | 现在 |
|---|---|---|
| 「广州天河」→ 参数「天河 广州」 | 放行（逐词都在） | `OPERATOR_TOOL_ARGUMENTS_INVALID` |
| 「深圳福田和广州南山」→「深圳 南山」 | 放行 | 拒绝 |
| 参数「广州 天河」→ 答复 `["广州","天河"]` | `OPERATOR_SITE_SEARCH_MISMATCH` | 通过；查询用原文「广州天河」 |
| 普通 nginx 加载模板 | `unknown directive "lua_package_path"` | 启动并存活 |
| `chSite/pageBOGUS` / `chSite/page/ORDER-1` / 缺单号的订单路径 | 放行 | 404 |
| 无订单回合调用站点工具 | 代理 500（`ToolDeclarationError`）；节点 `cpms_scope_mismatch` | 授权 → 取数 → 证据 → 完成 |

## 自动化

- `tests/operations/test_operator_site_tool.py`：重排/重组、文本订正、一致性检查仍在、
  答复与参数同一段。
- `tests/operations/test_operator_conversation.py::test_site_tool_end_to_end_through_the_real_tool_chain`：
  替身节点按**授权回执**取数与上报，最终正文来自本轮证据，CPMS 只被查一次且 `address=广州天河`。
- `tests/test_node_runtime_deploy.py`：真实 nginx 启动+存活、声明里每个工具可达、未声明形状拒绝、
  模板默认无 Lua、`--lua-directives` 原样注入。
- `tools/experiments/m2_close/m3node/CpmsToolAcceptance`：`PASS assertions=183`。

全量 `3838 passed / 9 skipped`；`ruff check` / `ruff format --check` 通过。

## 未完成

- **生产/真实业务验收未完成**：本轮未对 41 部署，未做账单、知识、两种风格、页面生命周期验收。
- 站点真实用户正例（授权账号）未执行；离线 CPPMS 为合成上游。
