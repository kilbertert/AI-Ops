# H5 聊天页 405 → 真实页面跑通：证据（2026-10-23）

承接 Codex 会话 `01a15cc6`（继续推进未完成业务目标）停住的那一步。

## 一、根因（比原先记的更彻底）

codex 停在「H5 的 POST 返回 405」。实测范围更大：**整片 AI 面从没到过网关**。

| 请求 | ulink.h5（修复前） | api.mall（对照） |
|---|---|---|
| `GET /v1/faq/recommendations` | **200 text/html**（index.html，gzip 780 B） | 401 JSON |
| `POST /v1/assistant/questions` | **405 nginx** | 401 JSON |

访问日志里真实用户那几次 `GET /v1/faq/recommendations 200 769` 不是成功 ——
769 与 `index.html` gzip 780 B 同量级，是 SPA 回退 HTML。用户唯一可见症状是
聊天页一句 **"Failed to ask"**。

## 二、修复

`ops/nginx/ulink-h5-assistant.conf` + `ulink-h5-assistant-proxy.inc`，
装到 41 的 vhost extension 目录，只转发 H5 聊天页**实际调用**的 12 条路径。
清单唯一声明处 `ops/nginx/h5-assistant-routes.txt`，
`tests/test_h5_assistant_routes.py` 用真 nginx 断言「声明==转发」且
「网关其余 76 条一条都不转发」。

**按方法拆 location**：`location ~` 不看方法，一条路径正则会顺带放开
`POST /v1/shortcuts`（管理面）。`limit_except` 也不解决问题（静态回退之后才生效）。

## 三、部署后宿主探测

```
GET  /v1/faq/recommendations   → 401 application/json  ← 到网关（修复前 200 HTML）
GET  /v1/shortcuts             → 401 application/json  ← 到网关（修复前 200 HTML）
POST /v1/assistant/questions   → 401 application/json  ← 到网关（修复前 405 nginx）
GET  /v1/agents                → 200 text/html         ← 仍不转发 ✓
POST /v1/shortcuts             → 405                   ← 仍不转发 ✓
```

## 四、真实页面复测（授权账号 13333555555，真实 H5 域名）

Playwright + 真实 H5 产物 + 真实登录会话，经 ssh -L 到 41 本机 443（保留原域名与证书）。

```
GET  /v1/faq/recommendations                     200 application/json  no-store
GET  /v1/shortcuts                               200 application/json  no-store  type=shortcut_list
POST /v1/assistant/questions                     202 application/json  no-store  type=qa  status=queued
GET  /v1/assistant/questions/qa_501b0734…       200 application/json  no-store  status=running  ×12
GET  /v1/assistant/questions/qa_501b0734…       200 application/json  no-store  status=completed
```

nginx 访问日志侧一致：running 轮询 `200 187`，末次 `200 641`（带 blocks）。
**修复前同路径是 `200 769` 的 HTML。** 页面从 "Failed to ask" 变成了正常渲染回答。

## 五、这次**没有**证明的

- **页面回答的业务内容**：账号有本人订单，但 `AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED`
  仍为**关闭**（Codex 那个受控窗口已恢复），所以模型如实回答「我不能查询具体订单」。
  这证明的是**路由通了**，不是**状态查询在页面可用** —— 那需要再开一次窗口。
- 聊天答案的业务准确性；App/BFF 接线；M3 整线。
- 公网真实用户（本轮全部经 41 本机回环）。

## 六、宿主上的变更与回滚

| 路径 | sha256 | 备份 |
|---|---|---|
| `/www/server/panel/vhost/nginx/extension/ulink.h5.mall.qushiyun.com/aiops-h5-assistant.conf` | `713dce44…` | `/root/aiops-h5-assistant.conf.installed-20261023` |
| `/etc/aiops-41/h5-assistant-proxy.inc` | `979197f5…` | `/root/h5-assistant-proxy.inc.installed-20261023` |

回滚：删掉这两个文件 → `nginx -t` → `nginx -s reload`。旧 vhost 与静态资源未改。
`/etc/aiops-41/gateway.env`、`production.env` 的 sha256 与窗口前基线逐字相同（未改）。
