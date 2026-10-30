# 站点成为管家工具（#1191）

分支 `feat/site-search-tool`，基线 `0b58d502`。

## 已完成

`cpms_site_search` 是这套机制的第一个真实用户：**无订单、带关键词参数、分页形状**。
它落在 ADR-0181 的既有两种响应形状里，所以节点侧只改了「参数怎么转给网关」一件事。

四处硬约束被这次落地撞出来，全部按声明改掉：

| 原来的硬写法 | 位置 | 现在 |
| --- | --- | --- |
| `operator-conversation/1` 必须无工具 | `execution_manifest` | 可以有，由声明的 `applies_to` 判 |
| 对话清单 `tools = []` | `operator_conversation.manifest` | 保留 `applies_to` 含 `conversation` 的工具 |
| 草稿工具列表 `max_length=2` | `OperatorPreviewDraft` | 来自声明 |
| 草稿工具名 `Literal["cpms_order_detail","cpms_charging_samples"]` | `OperatorPreviewTool` | 对着声明校验的字符串 |
| 发布期「参数必须为空对象」 | `node_publication.candidate` | 参数集合必须**恰好等于**声明的 `model_inputs` |

参数路径：节点把模型参数放进 authorization 请求体 → 网关按 ADR-0176 判据放行 →
网关物化请求目标。**放行发生在取到公司令牌之前**。

不重复取数：工具已取过站点时 `complete()` 复用那份证据；回答里的关键词与工具参数
不一致则拒绝（`OPERATOR_SITE_SEARCH_MISMATCH`）。

## 验证

- Python 全量 **3509 passed / 9 skipped**；ruff、format、compileall 通过。
- **Java 真编译 + 节点验收**：`CPMS_TOOL_ACCEPTANCE PASS assertions=153`（上一片 126）。
- **两侧声明对齐**：`TOOL_DECLARATION_PARITY PASS tools=3`。
- 新增 13 例覆盖参数三条判据、编造/复用地址、参数只进过滤器、协议边界。

## 缺口与边界

- **`NodeLinkAcceptance`（真平台派发整轮）仍未跑**，与 #1189 相同的边界。
- **无订单回合的站点渲染仍走两条路**：模型可以不调工具、直接返回 `kind=site_search`，
  这时网关照旧代查。两条路共用 `query_sites`，但「工具证据」与「代查结果」是两个入口
  共用同一份实现的两种形态，不是同一段代码路径。这一点在 ADR-0183 里写明了边界。
- **未完成业务验收**：没有真实 CPMS、没有真实账号、没有真实页面。

## 下一步

#1193（C 端站点工具与工具面归属）是这张票的姐妹片：C 端清单要能同时声明订单状态工具
与站点查询工具，那条硬编码的「状态工具不能与 CPMS 工具混用」要换成按运行类型的允许集合。
