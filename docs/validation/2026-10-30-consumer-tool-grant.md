# C 端工具授权的回归自检（#1209）

日期：2026-10-30。分支 `feat/consumer-tool-grant`，基线 `ebc56d5d`。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新判据 | `uv run pytest tests/consumer/test_consumer_tool_grants.py` | 7 passed |
| 全量 | `uv run pytest` | **3516 passed, 9 skipped** |
| 静态/格式 | `ruff check`、`ruff format --check` | 通过 |
| 镜像上下文摘要 | `context_digest.py --check` | match |

## 七条判据各自被证明

| 判据 | 断言 |
| --- | --- |
| 范围 = 本人订单站点 | 授权返回的 `site_ids` 等于直接调 `own_site_scope()` 的结果 |
| 只接受授权里那一个请求目标 | 同路径换查询串 → `CONSUMER_PATH_REJECTED` |
| 参数必须来自提问原文 | 「站点」通过，「深圳南山」被拒 |
| 运行类型不允许的工具被拒 | `cpms_order_detail` 在 C 端运行里 → `CONSUMER_TOOL_DISABLED` |
| 清单没勾的工具被拒 | 清单只声明状态工具 → `CONSUMER_TOOL_DISABLED` |
| 证据必须与网关投影逐项相等 | 改 total、改 source、少一个字段，三种分别拒 |
| 节点凭据 | 错 token → `CONSUMER_NODE_REJECTED` |

## #1191 的那个 bug 是怎么漏过去的

#1191 的 Java 验收断言了「模型参数到达网关」，Python 侧的合成节点却一直发 `{}`，
而端点的请求模型是 `extra="forbid"` 的空模型——**带参数就 422**。两边各自都是对的，
中间那一层没人测。

这是本仓第三次出现同一个形状：**断言的覆盖面比改动的面窄**（前两次是 #1191 的对话清单、
#1190 的声明挂载）。补法不是「再加一条断言」，是**改动的每一跳都要有一跳的断言**。

## 未完成业务验收

- CPMS 与订单列表都是合成的；没有真实账号、没有真实页面。
- **HTTP 接线只有单元级验证**：七条测试直接驱动 `ConsumerToolGrants`，没有跑通
  「节点 → 网关端点 → 代理」的真实 HTTP 链。本片没有对应的端到端 harness。
- C 端部署默认没挂工具声明（ADR-0184），现役 C 端要挂声明才有站点工具。
