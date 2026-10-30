# 站点工具落地的回归自检（#1191）

日期：2026-10-30。分支 `feat/site-search-tool`，基线 `0b58d502`。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新判据 | `uv run pytest tests/operations/test_operator_site_tool.py` | 13 passed |
| 管家与对话面 | `uv run pytest tests/operations/` | 见 PR |
| Dify 自助开通 | `uv run pytest tests/dify/test_self_service.py` | 32 passed |
| 全量 | `uv run pytest` | **3509 passed, 9 skipped** |
| 静态/格式/语法 | `ruff check`、`ruff format --check`、`compileall` | 通过 |
| **Java 编译 + 节点验收** | 锁定 JDK 镜像 + 锁定依赖 | `CPMS_TOOL_ACCEPTANCE PASS assertions=153` |
| **两侧声明对齐** | `tool_declaration_parity.py` | `PASS tools=3` |
| 镜像上下文摘要 | `context_digest.py --check` | match |

Java 断言从 126 涨到 153，差值覆盖：参数非空时的形状校验（五种非法形状在发请求前拒绝）、
参数原样转发给网关、以及既有的请求目标与混跑判据全部保持。

## 这次撞出来的四处「过期常量」

它们不是同一个 bug，而是同一个形状：**声明里加了一个工具，某个写死的常量过期了，
而报错看起来像「请求不合法」**。

| 常量 | 症状 | 现在 |
| --- | --- | --- |
| `OperatorPreviewDraft.tools` 的 `max_length=2` | 草稿试用 422 | 来自声明 |
| `OperatorPreviewTool.tool_name` 的 `Literal[...]` | 草稿试用 422，且信息是「Input should be …」 | 对着声明的字符串校验 |
| `operator-conversation/1` 必须无工具 | 对话回合拿不到工具 | 由声明的 `applies_to` 判 |
| 发布期「参数必须为空对象」 | 带参数的工具发布不了 | 参数集合必须恰好等于 `model_inputs` |

第三条是设计决定（ADR-0183），前三条是纯粹的过期常量。**它们值得记下来的理由是同一个**：
一个「加工具只改声明」的机制，如果不能在被违反时给出可读的错误，那它只是把改动点挪了个
位置。

## 一次真实的回归，被测试当场抓住

改完之后 `tests/dify/test_self_service.py` 12 个用例失败。根因不是产品代码：那个测试的
源应用 DSL 只声明了两个工具，而源应用的清单现在有三个 —— `draft_fingerprint` 前后不一致，
报 `SETUP_DRAFT_CHANGED`。它恰好说明「清单里的工具集来自源应用的勾选，不是代码里的默认值」
这条约束是真的。测试数据已补上第三个工具。

## 未完成业务验收

- 没有真实 CPMS、没有真实账号、没有真实页面。
- **CI 不构建 Java**：两条 Java 证据是手动跑的，不是门禁。
- `NodeLinkAcceptance`（真平台派发整轮）仍未跑。
- 无订单回合仍有一个不经工具的代查入口（模型直接返回 `kind=site_search`），
  它与工具共用 `query_sites`，但不是同一段代码路径 —— 这条边界写在 ADR-0183 里。
