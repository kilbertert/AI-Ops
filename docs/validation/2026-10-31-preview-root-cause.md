# 草稿预览失败的根因：插件 gateway_url 指向被拦截的公网路径（2026-10-31）

## 现象

签发真实授权后跑草稿预览，`knowledge` 节点失败：

```
检索当前用户适用知识 → ValueError:
知识目录或检索暂不可用，请核对工作区授权和发布状态
```

授权本身没问题（`grant.state=active`，`finally` 里已 `revoke`）。

## 根因

Dify 侧 `tool_builtin_providers` 里 `trendpower/consumer_knowledge` 的配置是：

```json
{"gateway_url": "https://api.mall.qushiyun.com", "catalogue_token": "…"}
```

公网入口实测：

| 路径 | 状态 | 含义 |
|---|---|---|
| `/v1/internal/consumer-knowledge/catalogue` | **403** | 公司网关拦截，到不了我们的 Gateway |
| `/v1/faq/recommendations` | 401 | 路由通、只是缺凭据 |

即：插件被配置成访问一个**公网侧不可达**的内部端点。
`/v1/internal/*` 是给**内网**用的（`ops/nginx/dify-consumer-callback.conf` 的精确 location），
它必须在 36 与 41 之间的内网链路，而不是公网域名。

## 为什么这不是本次改动引入的

`gateway_url` 是安装插件时写进 Dify 的配置，与提示词迁移无关。
本轮只签发了预览授权与发起只读草稿运行。

## 影响

**生产上任何 `knowledge` 类问答都会失败**，不只是预览。
但生产记录里 `chat_dify_runs` 最近的成功记录（10-30）`retrieval_status=found`，
说明当时这条链路是通的 —— 可能是之后变了，也可能成功记录走的是另一条路径。
这一点我**没有查清**，不据此下结论。

## 待确认

1. 36 → 41 的内网回调链路当前是否通（`ops/nginx/dify-consumer-callback.conf` 是否已装）。
2. `gateway_url` 应改成哪个地址（内网地址或带正确前缀的公网路径）。
3. 是否是最近某次部署把它改成了根地址。

修 `gateway_url` 属于**改生产 Dify 配置**，不在本次授权范围，需要单独决定。
