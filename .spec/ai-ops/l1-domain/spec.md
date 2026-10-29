---
title: l1-domain
desc: 纯函数领域规则——无状态、无依赖、无 IO
hue: 160
code:
  - src/aiops_diagnostics/identity/order_visibility.py
related:
  - src/aiops_diagnostics/data/order_status.py
  - src/aiops_diagnostics/health/rules.py
  - src/aiops_diagnostics/health/health_metrics.py
  - src/aiops_diagnostics/health/health_curves.py
  - src/aiops_diagnostics/health/health_report_copy.py
---
# l1-domain

业务规则本身：状态码的含义、停止原因的分类、可见性规则、健康指标算法。

## raw source

规则的来源必须是**后端源码或 SOP**，不能凭直觉写。这一层不 import 数据源、不读环境变量、
不发请求，因此可以在没有数据库、没有模型的测试里被完整驱动。

## 不变量

- **租户可见性只有一处定义**（见 [[order-visibility]]），三种渲染共用它。
- `order_status.role_of` 回答「是什么角色」（判定用），`rules.ORDER_STATUS` 回答「叫什么」
  （给人读）。同一份角色不再各处拼写。
