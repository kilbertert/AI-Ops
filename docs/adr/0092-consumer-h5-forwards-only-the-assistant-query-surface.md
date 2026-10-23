# consumer H5 只转发助手面，不整片代理 `/v1/`

日期：2026-10-23
状态：已采纳（转发面已可回归；**真实页面复测未完成**，见「后果」）

App 的聊天页在 `ulink.h5.mall.qushiyun.com`。它的 `POST /v1/assistant/questions`
返回 **405 的 nginx HTML**，请求根本没到网关。同一条路径在
`api.mall.qushiyun.com` 上是 **401 JSON**（真的到了网关，只是没带令牌）。

更完整的事实是：这个站点**整片 AI 面都没到过网关**。实测

```
GET  /v1/faq/recommendations   → 200 text/html   （= index.html，gzip 780 B）
POST /v1/assistant/questions   → 405 nginx       （静态文件服务不收 POST）
```

访问日志里真实用户那几次 `GET /v1/faq/recommendations 200 769` 也不是成功 ——
769 与 `index.html` 的 gzip 长 780 同量级，那是 SPA 回退的 HTML，不是推荐列表。
用户看到的唯一症状是聊天页一句 "Failed to ask"。

## 决定

在 AI-Ops 仓库维护一份 server 级 nginx include，装到该 vhost 已有的
`extension/ulink.h5.mall.qushiyun.com/` 目录，**只转发 H5 聊天页实际调用的那些路径**。
清单的唯一声明处是 `ops/nginx/h5-assistant-routes.txt`（12 条），
`tests/test_h5_assistant_routes.py` 用**真的 nginx**跑一遍并断言：

1. 声明的每一条都转发到了上游；
2. 网关 `/v1/` 下**其余 76 条**（含 `/v1/agents`、`/v1/knowledge/sources`、
   `/v1/dify/*`）一条都不转发。

按 Nginx 官方 `ngx_http_proxy_module` 与 `ngx_http_rewrite_module` 的依据：
正则 `location` 的 `proxy_pass` 不带 URI ⇒ 保留原请求路径；`proxy_set_header`
覆盖身份头；`proxy_next_upstream off` 让一次查询失败不自动重发
（诊断面是 `POST`，重发会造出第二个作业）；`proxy_cache off` + `no-store`
不缓存个人业务结果。

**按方法拆 location，而不是一条路径正则。** `location ~` 只匹配 URI、不看方法：
一条路径正则会把 `POST /v1/shortcuts`（创建快捷键，管理面）和
`GET /v1/assistant/questions`（那个方法返回列表，不是前端调的那条）一起放进代理。
`limit_except` 在这里不解决问题 —— 它在静态回退之后才生效，实测 `POST` 照样进了代理。
所以读的归读、写的归写，同一路径两种方法就拆两条 location。

身份仍是「服务令牌 + 用户自己的会话」：浏览器自报的 `Authorization`、
`X-Business-Entry`、`X-AIOps-Source-Key` 一律被覆盖，入口固定为 `consumer`。
权限由网关每次解析，nginx 不产生任何授权判断。

## 备选方案与后果

- **公司前端换 API 地址**：依赖公司发版，不满足本轮"不改公司仓库"的授权范围。
- **整片转发 `/v1/`**：顺带打开发布面、注册表面、Dify 运维面、知识源写入面，
  远超这条前端缺陷。判据实测会因此报出 26 条泄漏路径。
- **只转发 `assistant/questions` 一条**：`/v1/faq/*` 与 `/v1/shortcuts` 仍返回 HTML，
  页面会是"半坏"而不是修好 —— 症状比现在更难定位。
- **只在宿主 curl 验证**：证明不了用户页面可用。curl 只作诊断证据。

**这次没有证明的**：真实登录用户在页面里问出正确答案（页面复测未跑）；
聊天答案的业务正确性；App/BFF 接线；M3 整线。启用个人状态查询的窗口是
另一片（#976），本片不涉及。该入口仍可触发既有问答能力并花 provider 额度。

撤销：删掉本片两个文件后 `nginx -t` / reload，旧 vhost 与静态资源不动。
