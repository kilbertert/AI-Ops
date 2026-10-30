# C 端答复改版的 41 真机核对（未验成：该面在 41 上未启用）

日期：2026-10-30（UTC）。环境：41（生产）。方式：**只读**探测——读 Redis 会话、读业务库、
向公网入口发查询。未改任何配置、未启用任何开关、未写业务数据。

被验对象：#1175（`5de086c3`）+ #1202（`0b58d502`），决策见
[ADR-0182](../adr/0182-consumer-answers-lead-with-the-conclusion.md)。

## 部署事实（已核实）

| 项 | 证据 |
| --- | --- |
| 线上修订 | `curl http://172.18.0.1:8788/health` → `version=1.0.0+0b58d502adc5`，`business_mutations=disabled` |
| 新代码在位 | `/opt/aiops-41/src/aiops_diagnostics/consumer/consumer_plain.py` 可读，mtime `2026-10-30 13:48:12 +0800` |
| CD | run `41010446836` success |

## 探测方法（可复跑）

```bash
# ① 会话：从 41 的 Redis 取，令牌只在内存里传递，不落盘不打印
#    键前缀 = AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX（app:3rd_session:）
ssh aiops-41 'PW=$(grep -oE "^AIOPS_REDIS_PASSWORD=.*" /etc/aiops-41/production.env | cut -d= -f2 | tr -d "\"")
redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning --scan --pattern "app:3rd_session:*"'

# ② 冒烟：一次 200 才算会话有效
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "third-session: <会话>" -H "tenant-id: <会话租户>" -H "X-Business-Entry: consumer" \
  https://api.mall.qushiyun.com/v1/faq/recommendations

# ③ 该会话可归属的真实订单：ch_order_info 按 tenant_id + user_id 取（只读）
# ④ 用同一组头向 /v1/assistant/questions 提问，按 retry_after_ms 轮询
```

**两条头名事实（这次踩到的，写下来省一次弯路）**：

- 走**公网入口**（`api.mall.qushiyun.com`）时用 `third-session`（小写连字符）；BFF 会注入
  `Authorization: Bearer <服务令牌>` 并把它映射成网关的 `X-Third-Session`。
- **直达 `172.18.0.1:8788` 不行**：网关的依赖要求 `Authorization: Bearer`，只给
  `third-session`（或 `x-third-session`）一律 `401 ACCESS_TOKEN_REQUIRED`。本机调试要走公网入口，
  或者自己补上服务令牌。

## 结果：真实用户现在拿不到这条答复

用真实会话（`...687296` 租户，本人订单 `2115956502185209856`）实测：

| 提问 | 返回 |
| --- | --- |
| 查一下我的订单 2115956502185209856 现在是什么状态 | `404 TENANT_ENDPOINT_UNRESOLVED`（业务端点未登记） |
| 查一下订单 2115956502185209856 的账单 | `409 CHAT_ROUTE_UNAVAILABLE`（业务执行路由未配置） |

根因是开关而不是代码：`/etc/aiops-41/production.env` 里**没有**
`AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED`，也没有 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL`，
所以 `consumer_order_chat_enabled=False`（`gateway_config.py` 该键缺省即 `False`），
请求落到 `gateway_api.py` 的 `409 CHAT_ROUTE_UNAVAILABLE` 分支。

另有四条会话的 `userId` 缺失或该用户名下无订单，按「没有就如实记录、不编造」处理。

## 因此本轮**没有**证明什么

- **没有**证明 ADR-0182 的正文在 41 上出现了——那条面根本没被走到。
- **不能**用这次实测判断措辞好不好读：答复是错误码，不是我的正文。
- 真机／原 H5 上的实际观感（结论句是否在第一屏、`·` 与编号对齐）仍然**未验**。
- 九种非中文译文仍**未经母语者复核**。

## 要真验还缺什么

1. 决定是否为该租户启用 `AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED` 与
   `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL`。这是**开关变更**（对外行为变化），
   不属于文档片，需要单独授权与回滚方案。
2. 启用后用本节第 ②–④ 步重跑，并把两种答复的真实正文贴进验收记录。
3. 若要连"看起来怎么样"一起验，还需要一次真机截图（我看不到屏幕，只能验到文本）。

操作知识自检：本轮实际执行的步骤（会话取用、公网头名、开关判据）
已写进本节与 [41 手册](../agents/env-41-runbook.md) §4／§5 既有条目，不需要新命令文档。
未把任何会话令牌、口令或订单明文写进仓库。
