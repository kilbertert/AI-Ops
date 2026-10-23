# 跨站跳转的会话必须由目标域自己种：一次性票是唯一可行的中转

日期：2026-10-23
状态：已采纳
影响版本：PRD #981 的 #985；`/v1/internal/knowledge/open`、
`/v1/internal/knowledge/hop`、`knowledge_hop_tickets`、`ops/knowledge-console/`

## 背景

Dify 控制台在 `dify.ranlei.work`，RAGFlow 控制台在 `kb.mall.qushiyun.com`。用户在
Dify 的知识库页点「打开 RAGFlow」，要被送到后者并**已经登录**。

#984 的做法是：桥把网关给的 `target` 与 `cookies` 一起转给浏览器。

真机（2026-10-23）证明那样**不可能成功**：

```
桥（dify.ranlei.work）→ 303 + Set-Cookie: session=…; Domain=.mall.qushiyun.com
浏览器：拒收，且没有任何报错
```

定位过程本身就是一条教训：现象是"跳过去了、落在登录页"，日志里全是 200/303，
`Set-Cookie` 也确实发出去了。只有把响应头抓出来，才看得到属性完全正确而浏览器仍然
不保存 —— 因为**设置方不在那个域下**。

## 决定

**会话由 `kb.mall.qushiyun.com` 那一跳自己种下去，两跳之间传一张一次性票。**

```
Dify 页面 → 桥 → 网关 /v1/internal/knowledge/open   （内部令牌）
                    → 发一张票，回 target=…/__aiops/knowledge/hop?ticket=…
浏览器 → kb.mall.qushiyun.com/__aiops/knowledge/hop?ticket=…
         → 41 nginx 反代到网关
              → 兑换票（消耗）→ 换 RAGFlow 会话
              → 303 + Set-Cookie（**由 kb.mall 发出**）→ RAGFlow
```

票的形状：32 字节随机、60 秒、一次性、存 `knowledge_hop_tickets` 表、**只关联租户
与 workspace，不含任何会话材料**。

## 被否的三个方案

1. **桥直接转 `Set-Cookie`。** 就是上面被真机否掉的那个。cookie 规范不允许，
   没有配置能绕。
2. **把 RAGFlow 也搬到 `dify.ranlei.work` 的子域。** 那需要为它新增一个对外 TLS 站点
   与证书；而 `kb.mall` 已经存在且今天刚验过可用（#952）。
3. **把会话放进 URL 传给 RAGFlow。** 会话会进浏览器历史、Referer 与服务端日志。
   比"cookie 没生效"糟得多。

## 为什么票存数据库，不存进程内存

网关是多进程的。内存票会**随机**失效，而它的表现是"有时能进、有时不能" —— 那是这条
链路上最难排查的一种失败，且会被误判成"网络抖动"。一张 60 秒的表，代价可以忽略。

## 为什么兑换端点不接受内部令牌

它是**浏览器**的一次导航，带不了自定义头。它凭票自证：

* 票 32 字节随机、60 秒、一次性（`UPDATE … WHERE used_at IS NULL` 保证并发只有一次成功）；
* 票里没有会话，兑换时才用**那个租户自己的**凭据去换；
* 票由内部令牌那条路由发出，所以"谁在什么时候拿过票"仍然有据可查。

一个能拿到票的人，最多在 60 秒内让**自己**得到一个该租户的会话 —— 而票本来就是他
自己那一跳换来的。

## 一个刻意的冗余

`/v1/internal/knowledge/open` 在发票**之前**已经换过一次 RAGFlow 会话，而兑换时又换
一次。看起来浪费，但第一次的价值是**现在就知道能不能进**：未开户、上游不可达、凭据
解不开，都在这一步报出可区分的码，而不是让浏览器跳到 RAGFlow 再看到登录页。
RAGFlow 允许同一账号多会话并存，两次换出来的 cookie 互不影响。

## 回归

- `test_opening_returns_a_ticket_url_not_a_session_cookie` —— 应答里没有会话
- `test_the_hop_consumes_the_ticket_and_refuses_to_reuse_it` —— 用一次即废，第二次 410
- `test_an_unknown_or_cross_environment_ticket_is_gone` —— 不存在 / 跨环境同一个答案
- `test_the_ticket_carries_no_session_of_its_own` —— 票在库里只关联租户
- 网关库的 `consume_hop_ticket` 用同一条 UPDATE 判 `used_at IS NULL`，并发只有一次成功
