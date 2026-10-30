# 工具声明收口的回归自检（#1190）

日期：2026-10-30。分支 `feat/tool-declaration`，基线 `86da2776`。

本片把五处平行的工具定义收成一份声明。验收要证明三件事：**行为没变**、**两侧视图一致**、
**新增工具不需要改节点源码**。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 声明本身 | `uv run pytest tests/operations/test_operator_tools.py` | 14 passed |
| 管家面 | `uv run pytest tests/operations/` | 见 PR |
| 全量 | `uv run pytest` | **3468 passed, 9 skipped** |
| 静态/格式/语法 | `ruff check`、`ruff format --check`、`compileall` | 通过 |
| 镜像上下文摘要 | `context_digest.py --check` | match |
| **Java 编译 + 节点验收** | 锁定 JDK 镜像 + 锁定依赖 | `CPMS_TOOL_ACCEPTANCE PASS assertions=126` |
| **两侧声明视图对齐** | `tool_declaration_parity.py` | `PASS tools=2` |
| **新增工具不改节点源码** | 同上，用加了第三个工具的声明副本 | `PASS tools=3` |

Java 断言数与改造前**完全相同**（126）。这不是「没测到」，而是本片的目标之一：
声明换了宿主，行为不许动。

## AC2 的机械判据

「新增一个工具时节点源码目录零改动」如果靠人记得去看一眼 Java，它就是一条会过期的约定。
本片把它变成一条断言：

```
tests/operations/test_operator_tools.py::test_the_node_source_names_no_tool_at_all
```

逐文件扫描 `services/execution-node/src/main/java/**/*.java`，断言声明里的每个工具名都
不出现。改造前 `CpmsReadTool` 里有两个常量，这条断言会红；现在它绿，而且是**结构性地**绿：
节点不认识任何具体工具，它只认识 `single` 与 `page`。

第三份声明副本的对照跑（`tools=3`）是同一件事的端到端形态：两侧都接受一个从未存在过的
工具，节点源码字节不变。

## 未完成业务验收

- 没有真实 CPMS、没有真实账号、没有真实页面。
- **CI 不构建 Java**：两条 Java 证据是手动跑的，不是门禁。这既是既有缺口，也是本记录
  必须写清楚的一条——否则下一个人会以为 CI 在看着。
- 不能引用本记录说明「新增工具已经跑通真实接口」：本片只证明定义收口与两侧一致性。
