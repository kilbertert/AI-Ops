# 生产知识库授权核对（2026-10-31，只读）

目的：证明目标租户在生产上的**真实**知识库所有权与对客授权，不用任何合成夹具。

## 方法与入口

经生产网关的受管目录接口 `GET /v1/internal/consumer-knowledge/catalogue`，
凭据按 `consumer_knowledge_selection.catalogue_token` 派生
（`ckc_<b64 workspace>.<hmac("consumer-kb-catalogue:1:<env>:<workspace>")>`）。

**两个容易错的地方**（本轮实测踩到：
- 派生用的 `environment` 是 `AIOPS_GATEWAY_ENVIRONMENT=41`，
  而授权表里的 `environment` 是 `AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=production-41`；
  两者不同名，混用会得到 403 `KNOWLEDGE_CATALOGUE_FORBIDDEN`。
- workspace 必须取目标应用的登记 workspace `c3f871e1-ffd0-4a10-a84a-b8696f7acd25`，
  不是 `AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID`。

## 结果

目录返回该租户 **5** 个知识库；其中对客授权（`consumer_knowledge_grants`
`tenant=1942105476598861824`、`environment=production-41`、`revoked_at IS NULL`）**4** 个：

```
granted  188c54f4d52111f1a026f1f706dc4043   对客知识库-a9a6bd4cf6075f6dd1e7d2bc630764c8
granted  38676440d37c11f1a026f1f706dc4043   shiping1
granted  d340d150d2b311f1a026f1f706dc4043   zhishiku
granted  3064598cd01c11f18637d95f7710e3a3   WF01-C端只读指导-审核V1-20261025
internal 5c9313bccdfc11f18637d95f7710e3a3   41(1)        ← 在目录里但不是对客授权
```

`41(1)` 出现在目录但**没有对客授权行**，属于内部库。按
`consumer_knowledge_selection.require_owned` 的 fail-closed 规则，它不能交给消费者；
这不是缺陷，是设计（缺授权行一律按内部处理）。

另有 4 条 `revoked_at` 非空的授权（`4129e1b6…`/`c1edcc54…`/`ed1c1412…`/`497eb04c…`），
现已撤销，不会出现在对客集合里。

## 边界

本次只证明**网关侧**的租户所有权与对客授权。它不证明：
- kb-service 上游对这些库的存活与检索质量；
- 目标应用节点上实际选中的是哪一个（应用的 `knowledge` 节点用
  `data_grant` 动态选择，不是写死库 ID）。

上述两点要在真实预览/正式会话里观察，不能由本核对代替。
