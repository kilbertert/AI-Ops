# C 端运行的 CPMS 工具授权（#1209）

分支 `feat/consumer-tool-grant`，基线 `ebc56d5d`。

## 已完成

C 端运行现在能真的调用 `cpms_site_search` 并取到数。**节点一行没改，契约没升版。**

关键事实：节点把请求发到**网关代理**，带的是网关签发的 grant token；公司令牌从头到尾只在
网关手里。所以这不是「给 C 端建一套协议」，是「网关多一条授权与代理路径」。

- `consumer/consumer_tool_grants.py`：授权、代理、证据三步，**不持有任何业务凭据**。
- 授权端点按 run 自己的 `operation` 分派；代理端点是新的
  `/v1/internal/consumer-tools/{run_id}/{path}`，`base_url` 取自节点目标的 `proxy_base_url`。
- 范围推导用 `consumer_sites.own_site_scope`、单页校验用 `operator_cpms.site_page` ——
  两个都是既有实现，C 端没有第二份（这就是 #1193 的 AC4）。

## 顺手修掉 #1191 的一个真 bug

#1191 让节点把模型参数放进 authorization 请求体，但那个端点的请求模型是
`extra="forbid"` 的空模型：**带参数就 422，而且参数从来没被读出来**。合成节点一直发
`{}`，所以那一批测试全绿——**断言的覆盖面比改动的面窄，又一次**。

现在请求体是 `OperatorToolAuthorizationRequest`，只允许 `arguments`，空体仍然合法。
这条同时修好了管家侧的站点工具。

## 验证

- Python 全量 **3516 passed / 9 skipped**；ruff、format 通过。
- 新增 7 条覆盖：范围等于 `own_site_scope`、请求目标白名单、参数必须来自提问原文、
  运行类型不允许的工具被拒、清单没勾的工具被拒、证据逐项相等（含三种破坏）、节点凭据。

## 缺口与边界

- **未完成业务验收**：CPMS 与订单列表都是合成的；没有真实账号、没有真实页面。
- **HTTP 接线只有单元级验证**：七条测试直接驱动 `ConsumerToolGrants`，没有跑通
  「节点 → 网关端点 → 代理」的真实 HTTP 链。这条缺口来自本片没有对应的端到端 harness，
  不是有意省略。
- C 端部署默认**没有**挂工具声明（ADR-0184），所以 C 端站点工具在现役部署里要挂了声明
  才可用。
