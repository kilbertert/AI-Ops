---
title: l6-boundary
desc: 进程边界与入口——四条独立的进程、63 条路由
hue: 0
code:
  - src/aiops_diagnostics/gateway_api.py
related:
  - src/aiops_diagnostics/gateway_runtime.py
  - src/aiops_diagnostics/cli.py
  - src/aiops_diagnostics/gateway_server.py
  - src/aiops_diagnostics/gateway_cli.py
  - src/aiops_diagnostics/gateway_client.py
  - src/aiops_diagnostics/gateway_config.py
  - src/aiops_diagnostics/gateway_tokens.py
  - src/aiops_diagnostics/gateway_logging.py
  - src/aiops_diagnostics/admin_cli.py
  - src/aiops_diagnostics/tdengine_proxy.py
  - src/aiops_diagnostics/responses_adapter.py
  - src/aiops_diagnostics/codex_launcher.py
---
# l6-boundary

四个 console script 对应四条独立的进程边界：`aiops`（工程师本地）、`aiops-gateway`（多端服务）、
`aiops-tdengine-proxy`（只读 SQL 代理）、`aiops-responses-adapter`（兼容反向代理）。

## raw source

**凭据不能到达客户端，所以有 Gateway**。密钥留在服务端，客户端只持有一个可吊销的注册码。
把密钥下发到客户端换取的任何便利，都要用"一次泄露即全量泄露"来计价。

## 不变量

- **权限必须经 `ScopeContext`**，不能从请求体取 `user_id`/`tenant_id` 当授权依据。
- **新增读取入口默认关闭，且不得自带一份授权。** `GET /v1/orders/{order_no}/charging-status`
  在未配置 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 时按 404 处理（路由不存在），
  配置后也只接受 consumer 的非代查本人范围，并**复用** [[l1-domain/order-visibility]]
  的既有判定后才允许打业务接口；越权与不存在同为 404，授权源不可用是 503。
  `Cache-Control: no-store` 加在这一组路径的成功与错误响应上。
- `tdengine_proxy` 是**严格只读白名单代理**，且只监听回环：它是 SQL 进入 TDengine 的唯一门。
- 适配器对客户端**完全透明**：它修复上游的两个兼容缺口，不引入新语义。
- **日志配置必须显式**：在这一层出现之前，网关根本没配过 logging——uvicorn 只给自身挂 handler，
  root 停在 WARNING 且无 handler，于是 `aiops.*` 的 INFO 一行都没出去过（实测 0 行）。
  给 root 挂 handler 是这一层的职责，且级别写错即启动失败。
