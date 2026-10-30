# 一个工具是什么，写在声明里（#1190）

分支 `feat/tool-declaration`，基线 `86da2776`。

## 已完成

`src/aiops_diagnostics/operator/operator_tools.json` 是「一个 CPMS 只读工具是什么」的
唯一声明。五处曾各写一份的东西现在都从它读：

| 曾各写一份的地方 | 现在 |
| --- | --- |
| 网关的路径表 `PATHS` | `tool_request()` 用声明的 `path_template` 物化 |
| 网关的证据字段表 `DETAIL_FIELDS` / `SAMPLE_FIELDS` | 声明里的 `evidence_fields` |
| 发布白名单 `OPERATOR_TOOL_NAMES` | 声明里的 `tools[].name` |
| provider 的 OpenAPI 生成 | 声明里的名称、描述与空参数集合 |
| 节点侧的装配与取证投影 | `OperatorTools` 读同一份文件；按 `response.kind` 分派 |

节点源码里**一个工具名都没有**。这不是巧合，是一条断言的直接结果：
`test_the_node_source_names_no_tool_at_all` 逐文件扫描 `services/execution-node/src/main/java`
并断言声明里的每个名字都不出现。于是「新增工具要改 Java」不是「不该做」，而是做不到。

`aiops admin ensure-node-provider` 增加 `--migrate`：工具集变了时 provider schema 一定会变，
这条是它的显式出路，而不是「schema 不同即抛错」当唯一答案。

## 验证

- Python 全量 **3468 passed / 9 skipped**；ruff、format、compileall 通过。
- **Java 真编译 + 节点验收**：`CPMS_TOOL_ACCEPTANCE PASS assertions=126`（与改造前同值——
  行为未变，这正是 AC6 要的）。
- **两侧逐项对齐**：`tool_declaration_parity.py` 用同一份声明比对 Python 与 Java 的视图，
  `TOOL_DECLARATION_PARITY PASS tools=2`。
- **AC2 的形态证明**：拿一份**多加一个工具**的声明副本再跑一次对齐，
  `TOOL_DECLARATION_PARITY PASS tools=3` —— 两侧都接受，节点源码一个字节未改。

## 缺口与边界

- **仓库 CI 不构建 Java**，所以上面两条 Java 证据是**手动**的，不是门禁。这是既有缺口
  （#1189 记录过），本次没变好也没变坏；写进 ADR 是因为一个不跑的检查被当成门禁比没有更糟。
- **声明表达不了任意接口**：只允许 GET，响应只有 `single` 与 `page` 两种形状。第三种形状
  要先扩引擎再写声明 —— 这一点写在声明的 `_comment` 里。
- **发布清单的字节与身份不变**：清单里的描述仍来自部署模板，声明决定的是白名单、provider
  与节点装配。改描述会改清单 sha256 并要求重发，所以在「一份真源」与「不改已发布身份」
  之间选了后者。
- **未完成业务验收**：没有真实 CPMS、没有真实账号、没有真实页面。

## 下一步

#1191（站点工具·管家侧，含参数放行）已解除阻塞。那一张是这套机制的第一个真实用户：
一个带参数、无订单、分页形状的新工具 —— 加它应该只动一条声明。
