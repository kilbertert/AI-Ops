# 站点工具按站点身份校验，不要求订单号（2026-11-02）

分支 `fix/site-tool-no-order-identity`，基线 `2a59a18e`。

## 现象

`cpms_site_search` 在 41 上**每次都失败**，错误码 `cpms_unavailable`——看起来像网络故障。

```
000 = 节点容器 → api.mall.qushiyun.com（连不通）
```

但这条线索是**误导**的：网关早就把 `base_url` 指向了内部代理
（`operator_cpms.py` 第 589 行：`task.proxy_url + "/v1/internal/operator-runs/{run}/cpms"`），
容器到 `172.30.41.1:8788` 是通的（404 = 连上但路由不匹配）。

## 根因：`identity()` 要求订单号

`cpms_unavailable` 是 catch-all，把真实异常吞掉了（有意为之：上游正文、地址与令牌不进日志）。
临时加日志后拿到：

```
CpmsRefused: cpms_scope_mismatch
  at identity(CpmsReadTool.java:224)
```

`identity()` 对**每一行**都要求：

```java
row.path("orderNo").isTextual() && grant.orderNo().equals(row.path("orderNo").asText())
```

而站点行是 `{id, name, address, tenantId}`——**没有 `orderNo`**。站点工具是一次无订单的
会话回合，它的身份是**站点**，不是订单。

同类问题还有两处：

| 位置 | 问题 |
| --- | --- |
| `Grant` 构造器 | `inputs.isEmpty()` 直接拒绝——站点工具的 `inputs` 是 `{}`（关键词可为空） |
| `Grant.orderNo()` | `order_no` 缺失即抛 `invalid_authorization` |

## 修复

1. `Grant.orderNo()` 在 `order_no` **缺失**时返回空串（有值仍校验形状）
2. `Grant` 允许 `inputs` 为空对象
3. `identity()` 按**有没有订单号**分流：
   - 有订单：行必须就是这一单、这一个站点（原判据不变）
   - 无订单（站点工具）：行的身份是站点 id（`siteId` 或 `id`），且必须在这份授权允许的集合里

判据与网关侧 `site_page` 一致——它读的是 `item["id"]` 与 `item["tenantId"]`。

## 验证

新增验收 `CpmsToolAcceptance.siteSearchHasNoOrderIdentity()`：

- 无 `order_no` 的授权 + 站点行 → **必须通过**，且证据里带 `id=S1`
- `site-wrong-tenant` / `site-wrong-site` → 拒绝且不记录证据

```
CPMS_TOOL_ACCEPTANCE PASS assertions=195 http_calls=16
```

**变异验证**：把 `identity()` 恢复成无条件要求 `orderNo`，重编后该用例在
`CpmsReadTool.samples:210` 处失败。恢复后全绿。

## 一个值得记的排障弯路

我先看到容器连不通公网，就判定「节点需要公网出口」。实际网关早已返回代理地址，
**`cpms_unavailable` 是 catch-all，把 `cpms_scope_mismatch` 掩盖成了网络故障的样子**。
加一行临时日志就把方向纠正了——**catch-all 存在时，先确认真实异常再下结论**。

## 未做

未部署。需要重新构建节点 jar 并按「仅更新 Java 节点 jar」流程发版。
