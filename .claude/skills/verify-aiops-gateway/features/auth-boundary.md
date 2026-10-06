# 认证与角色边界

## Sub-features

- 无令牌一律 401（`ACCESS_TOKEN_REQUIRED`）
- 设备令牌可访问 run 路由（`VIEW_ROLES`）
- 管理/编辑路由需要 UPMS 角色（`EDIT_ROLES`），设备令牌被拒

## How to get to it (user POV)

不是用户入口，而是**每条路由的准入规则**。任何鉴权相关改动都应在这里验证失效路径。

## Driving it

```bash
BASE=http://127.0.0.1:8787

# 1. 无令牌：必须 401，且错误体是标准信封
curl -s $BASE/v1/runs
# {"error":{"code":"ACCESS_TOKEN_REQUIRED","message":"access token required","retryable":false}}

# 2. 设备令牌：run 路由放行
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" $BASE/v1/runs
# 200

# 3. 同一个设备令牌：管理路由仍拒绝
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" $BASE/v1/agents
# 401 — 需要 UPMS 角色，设备注册不会授予
```

## Gotchas

- **`/v1/agents` 的 401 不是缺陷。** 它需要 `EDIT_ROLES`/`VIEW_ROLES` 里的 UPMS 角色；
  本地 `issue-enrollment` 注册的设备拿不到。**在把 401 报成 bug 之前先确认该路由要哪个角色。**
- **401 的响应体是契约的一部分。** 断言状态码之外还要断言错误信封形状
  （`error.code` / `message` / `retryable`）——只断言 401 会漏掉错误体退化。
- **`/health` 是唯一免认证的路由**，它返回 200 与鉴权无关。用它判断"服务起来了"，
  不要用它判断"鉴权正常"。
- 公司侧 `/oauth/check_token` 有个反直觉之处：**失败体走 HTTP 200**，成功体反而没有
  `code`/`data` 信封。"HTTP 200 就代表令牌有效"在这里不成立 —— 涉及该链路时要断言**响应体
  的身份字段**，不能只看状态码。
