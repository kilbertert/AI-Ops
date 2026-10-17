---
title: l0-contracts
desc: 共享词汇与基础设施——被所有层导入，因此必须零业务知识
hue: 200
code:
  - src/aiops_diagnostics/agent_contracts.py
related:
  - src/aiops_diagnostics/models.py
  - src/aiops_diagnostics/config.py
  - src/aiops_diagnostics/bounded_http.py
  - src/aiops_diagnostics/redaction.py
  - src/aiops_diagnostics/i18n.py
  - src/aiops_diagnostics/private_files.py
  - src/aiops_diagnostics/platform_paths.py
  - src/aiops_diagnostics/http_auth.py
  - src/aiops_diagnostics/console_encoding.py
  - src/aiops_diagnostics/render.py
  - src/aiops_diagnostics/jev_decisions.py
---
# l0-contracts

这一层被所有人导入，所以它只能放**共享词汇**，不能放编排或流程。

## raw source

判据是入度而非直觉。`agent_contracts.py` 的入度是 12，`agent_engine.py` 的入度是 1——
契约是共享词汇，编排是流程，两者不在同一层。分层是为理解服务的，不是为整洁服务的。

## 不变量

- **所有出站 HTTP 只有一条骨架**（`bounded_http.py`）：请求构造 → 认证头 → `urlopen` →
  异常分类 → 信封解析。各调用点用声明式参数注入自己的错误类、认证方式与重试策略，
  差异因此显式可见；另写一份出站客户端属于缺陷。
- **`i18n` 是纯头部解析**，结果只用于呈现。它不得改变权限判定或路由——一个来自请求头的
  值若能影响授权，等于把授权交给调用方。
- **`config.Settings` 是全部配置的单一来源**，含 `SafetySettings`（上限）与
  `AgentSettings`（provider 注册表）。上限是设计的一部分，放宽需说明理由。
- 凭据只透传给目录服务，绝不进入日志、审计摘要或错误消息。
