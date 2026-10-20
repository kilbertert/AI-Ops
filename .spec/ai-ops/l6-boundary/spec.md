---
title: l6-boundary
desc: 进程边界与入口——四条独立的进程、66 条路由
hue: 0
code:
  - src/aiops_diagnostics/gateway_api.py
related:
  - src/aiops_diagnostics/gateway_runtime.py
  - src/aiops_diagnostics/cli.py
  - src/aiops_diagnostics/gateway_server.py
  - src/aiops_diagnostics/isolation_guard.py
  - src/aiops_diagnostics/gateway_cli.py
  - src/aiops_diagnostics/gateway_client.py
  - src/aiops_diagnostics/gateway_config.py
  - src/aiops_diagnostics/gateway_tokens.py
  - src/aiops_diagnostics/gateway_logging.py
  - src/aiops_diagnostics/admin_cli.py
  - src/aiops_diagnostics/tdengine_proxy.py
  - src/aiops_diagnostics/responses_adapter.py
  - src/aiops_diagnostics/codex_launcher.py
  - src/aiops_diagnostics/execution_manifest.py
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
- **试运行实例必须在打开任何库之前证明自己隔离**。`gateway_server serve` 读到
  `M2LIVE_ISOLATION_ROOT` 时，先由 `isolation_guard` 核对可写状态、只读业务依赖与运行 UID；
  任一条不成立就不监听端口。未设置该变量时门禁是空操作，生产启动路径逐字不变 ——
  这条区别本身就是「试运行实例不能写生产库」的机制，而不是一段文档承诺。
- **执行配置身份在签发时固定、领取时对照，且未启用时逐字不变**。配置了
  `AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE` 的网关把清单的**原始字节 sha256** 写进 run 记录；
  节点领取时必须回传同一身份，不等或缺失 ⇒ 记录**落终态**（`failed` + `reason=exec_config_drift`）
  且**不返回快照**（三条放行路径共用同一段判定，含首次领取、成功快照重试、竞争后放行）。
  未配置时行为与今天逐字一致 —— 这由 GW-04 钉住，而不是一句承诺。
  列变更走幂等真迁移，旧记录保持 `null`，不回填。
  签发时重读清单只影响新 run；已有 run 继续对照记录中的期望，清单轮换不等于撤销已有 run。
  **身份比较、记录写入与据以放行的状态必须同处一个写事务**：写不进去就是不放行，
  返回给调用方的必须是写入之后读到的记录，否则另一请求的拒绝提交后仍会放出快照。
  最终放行事务提交是判定时点；此前提交的撤销必须拒绝，此后提交的撤销阻止后续领取，
  不承诺撤回已经获准发送的响应。
  **有效期在同一事务内按既有语义核对**（`expires_at <= now` 即过期，复用读路径的收敛语句）：
  拿记录的那一刻有效不等于放行这一瞬间有效，只查状态不查有效期会让过期记录照样交出正文。
  **「已固定期望」不等于「已核对成功」**：回执的 `agent_version` 只在核对一致后给出，
  未核实时为 `null` 且显式标缺口。见 ADR-0052 与 `execution_manifest`。
- **领取成功不等于整轮完成**（M3-NODE-LINK-01）。启用清单的 run 在领取并缓存快照后
  **仍是 `running`**；只有节点用
  `POST /v1/internal/charging-status/completions/{run_id}` 显式确认、且回传的身份与本
  run 签发时固定的期望逐字相等、记录仍在 `running` 且已有快照、此刻未过期，才转
  `succeeded`。取消与进程退出没有回调 ⇒ 停在 `running` 由既有 TTL 收敛为 `expired`，
  **`running` + 快照永远不会被读成成功**。未启用清单时该端点回
  `409 AGENT_RUN_COMPLETION_NOT_TRACKED` 且**一个列都不改**；已完成的记录不被迟到通知
  改写；同状态不同原因的通知是冲突而不是幂等。
  **默认关闭不等于库无变化**：初始化即 `ALTER TABLE` 补列，部署影响需单独评估。
- 适配器对客户端**完全透明**：它修复上游的两个兼容缺口，不引入新语义。
- **日志配置必须显式**：在这一层出现之前，网关根本没配过 logging——uvicorn 只给自身挂 handler，
  root 停在 WARNING 且无 handler，于是 `aiops.*` 的 INFO 一行都没出去过（实测 0 行）。
  给 root 挂 handler 是这一层的职责，且级别写错即启动失败。
