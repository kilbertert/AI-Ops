# 工具面判据收敛的回归自检（#1193）

日期：2026-10-30。分支 `feat/tool-surface-by-run-type`，基线 `be78e7c2`。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量 | `uv run pytest` | **3508 passed, 9 skipped** |
| 静态/格式 | `ruff check`、`ruff format --check` | 通过 |
| **Java 编译 + 节点验收**（有声明） | 锁定 JDK 镜像 + 锁定依赖 | `CPMS_TOOL_ACCEPTANCE PASS assertions=166` |
| **Java 验收**（无声明，模拟现役 C 端部署） | 同上，不传 `-D` | `CONSUMER_SURFACE_ACCEPTANCE PASS assertions=4` |
| 镜像上下文摘要 | `context_digest.py --check` | match |

Java 断言从 153 涨到 166，新增十三条全部是本片的判据，没有一条是把旧断言换成松的。

## 三条判据各自被证明

| 判据 | 断言 |
| --- | --- |
| C 端清单可同时声明状态工具与站点工具 | `ToolSurface.allowed("m3-exec-config/1")` 同时含两者，且不含订单诊断工具 |
| 状态工具与订单诊断工具仍不能混用 | `allowed("operator-exec-config/1")` 不含状态工具；对话运行只含站点工具 |
| 不允许的工具被拒且拒绝码稳定 | 未声明工具在三种运行类型下都不成立；未知运行类型抛 `ExecConfigViolation` |

外加清单解析层：对话清单接受站点工具、拒绝订单工具（`ExecManifest.fromText`）。

## code review 抓到的一处回归，已修

第一版让 `ToolSurface.allowed()` **无条件**读工具声明，而声明既没打进 JAR 也没挂进
`deploy/node-runtime/compose.yaml`。现有 C 端节点的清单里只有状态工具，它们会因为一个
用不上的文件在启动/解析时失败。

修法不是「把声明也挂给 C 端」，而是**让声明只在对它有用的运行类型上必需**：

| 运行类型 | 没有声明时 |
| --- | --- |
| `m3-exec-config/1`（C 端） | 退化成只有状态工具（旧行为），不失败 |
| `operator-exec-config/1` / `operator-conversation/1` | **必须**失败，且信息里指出 `-Daiops.node.tools-file` |

`ConsumerSurfaceAcceptance` 在**不带** `-D` 的进程里跑这四条断言，两个方向都覆盖。

## 上一片漏掉的一处，被这次发现

`ExecManifest.parse` 里还留着 `if (conversation && !tools.isEmpty())` 的硬约束。
ADR-0183 只改了 Python 侧，而上一片的 Java 验收**不解析对话清单**，所以那条断言从未
覆盖到这个分支。**这是「验收覆盖不到就不会红」的一个实例**：不是忘了写断言，是断言的
覆盖面比改动的面窄。

## 未完成业务验收

- 没有真实 CPMS、没有真实账号、没有真实页面。
- **AC1 的「两者都能被调用」只做到装配面**：C 端站点工具取不到授权，因为 C 端运行的
  授权路径不存在。已另立票。
- **AC4（与只读接口范围一致）随同一条阻塞**，不记为满足。
- **CI 不构建 Java**：166 条断言是手动跑的。
