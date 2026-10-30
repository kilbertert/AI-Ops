# 站点查询预重构的回归自检

日期：2026-10-30。分支 `refactor/site-query-seam`，基线 `42884f1b`（PRD #1183 的子单 #1187）。

本轮**不改变任何对外行为**，因此验收判据只有一条：现有断言不改，仍然全绿。下面是这条
判据的取证方式与结论。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量测试 | `uv run pytest` | 3432 passed, 9 skipped |
| 管家模块测试 | `uv run pytest tests/operations/` | 247 passed, 2 skipped |
| 静态检查 | `uv run ruff check .` | 通过 |
| 格式 | `uv run ruff format --check .` | 通过 |
| 语法 | `uv run python -m compileall -q src tests` | 通过 |
| 镜像上下文摘要 | `uv run python deploy/m2-live-03/context_digest.py --check` | match |

## 断言是否被改动

**没有。** 唯一的测试改动是四处调用点的传参形状：
`search_sites(answer, caller, base, bearer, auth)` → 调用壳 `site_query(caller, value)`。
每一处对应的 `assert` 文本逐字未动，包括 URL 查询串的逐项比对、`PRIVATE-PHONE` 不能出现、
`calls == [1, 2, 3, 4]` 的翻页上限、以及各个拒绝分支的 `pytest.raises`。

## 新增的两例

对应 #1187 的验收标准「范围为空、范围未限定、关键词命中、关键词无命中四种情形各有一例」中
原本缺的两项：

- `test_unbounded_scope_is_not_filtered_and_says_so_in_the_count`：顶层账号的
  `site_ids is None` 必须**不过滤**且 `scope_limited is False`。它与 `site_ids == ()`
  是相反的两件事，混用会让空范围账号看见整个租户。
- `test_a_search_without_a_match_is_empty_not_an_error`：搜索没命中是「0 条结果」，
  不是错误；`total` 仍取 CPMS 自己的计数。

## 一条需要说明的噪声

编辑期出现的两条 Pyright 诊断（`operator_conversation` 可能未绑定、`body` 的类型推断）
经核对属于 **canonical checkout**（另一会话的分支副本），不在本 worktree；仓库 CI 也不
运行 Pyright（`ci.yml` 只跑 ruff / pytest / compileall）。其中「可能未绑定」那条已因本片
把惰性导入收成模块级一处而消失。

## 未完成业务验收

- 没有真实 CPMS 调用、没有真实账号、没有真实页面。
- 本片不新增任何业务能力：站点仍然只在对话回合里可查，仍然只在管家入口可用。
- 因此**不能**引用本记录去说明「站点已封装为工具」「用户端已可用」——那两件事分别在
  #1191 与 #1192，都还没开始。
