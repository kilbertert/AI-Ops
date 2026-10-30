# 工具面由运行类型决定（#1193）

分支 `feat/tool-surface-by-run-type`，基线 `be78e7c2`。

## 已完成

`ToolSurface.allowed(schema)` 是「这一次运行允许哪些工具」的唯一实现，清单解析与工具
装配都调它。原先按工具名硬编码的互斥删掉——它表达的意思仍成立（状态工具与订单诊断工具
不能共存），但那现在是允许集合的**结果**。

| 运行类型 | 允许的工具 |
| --- | --- |
| `operator-exec-config/1`（订单诊断） | `applies_to` 含 `order` 的 CPMS 工具 |
| `operator-conversation/1`（无订单回合） | 含 `conversation` 的 CPMS 工具 |
| `m3-exec-config/1`（C 端） | 状态工具 + 含 `conversation` 的 CPMS 工具 |

## 顺手补掉上一片漏的一处

`ExecManifest.parse` 里还有一份同样的硬约束：

```java
if (conversation && !tools.isEmpty()) throw "非订单回合不允许订单工具";
```

ADR-0183 放开无订单回合带工具时只改了 Python 侧，这一处没改，而上一片的 Java 验收
**不解析对话清单**，所以没抓到。本片把它并进 `ToolSurface`，并补了解析层的断言：
对话清单接受站点工具、拒绝订单工具。

## code review 抓到的一处回归

第一版无条件读工具声明，而声明没打进 JAR、也没挂进 compose。现有 C 端节点（清单里只有
状态工具）会因此起不来。已改成「声明只在对它有用的运行类型上必需」：C 端没有声明时退化
成只有状态工具，operator 两类运行没有声明时必须失败并指出怎么修。

## 验证

- Python 全量 **3508 passed / 9 skipped**；ruff、format 通过。
- **Java 真编译 + 节点验收**：`CPMS_TOOL_ACCEPTANCE PASS assertions=166`（上一片 153）；
  另加 `CONSUMER_SURFACE_ACCEPTANCE PASS assertions=4`，在**不带声明**的进程里证明现有
  C 端部署照旧可用（code review 抓到的回归，见下）。
  新增覆盖：三种运行类型的允许集合、未声明工具在三类运行下都被拒、未知运行类型被拒、
  对话清单接受站点工具而拒绝订单工具。

## 缺口与边界

- **AC1 的「两者都能被调用」只做到装配面。** C 端清单可以声明状态工具 + 站点工具，
  节点会装配两者，但站点工具在 C 端运行里**取不到授权**：`operator_cpms._current` 只接受
  两类 operator 运行，C 端运行的授权路径（任务注册表、范围推导、证据比对）不存在。
  已另立票跟踪，本片不假装它成立。
- **AC4 同理**：C 端工具取不到数，就没有「与只读接口一致」的范围可比。等授权路径落地后
  由那条票证明。
- **CI 不构建 Java**：166 条断言是手动跑的。
- **未完成业务验收**：没有真实 CPMS、没有真实账号、没有真实页面。
