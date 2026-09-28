# 「管家端 vs 客户端」是怎么区分的：三套 `client-type` 取值不要混用

> **读者**：接 AI-Ops 的前端 / BFF 同学，以及排查"为什么走了另一套范围"的人。
> **状态**：2026-09-28 实测（41 生产 + 公司 GitLab 源码 + 前端 bundle 逆向）。
> **一句话**：**区分管家端与客户端的是 `X-Business-Entry`，不是 `client-type`**；
> 而 `client-type` 这个词在三个地方指三套不同的取值，**当前链路上它们是断开的**。

---

## 1. 先说结论：谁是入口的判据

`X-Business-Entry: consumer | operator` 是**唯一**决定"用哪套内容域与哪套订单可见范围"的头。

- 值域只有两个：`consumer`（客户端）、`operator`（管家端）。
- 大小写与首尾空白会被规范化（`OPERATOR` / `" operator "` 都认），与网关内部
  `PlatformIdentityResolver` 的判法一致。
- **缺失或非法值不会被当作"管家端"**：身份层按**最窄**的本人范围处理，
  随后平台决策会把非法值直接拒掉（`403 PLATFORM_FORBIDDEN`）。
- 41 的 Nginx 对它做了兜底（见 §4）：**没带就当作 `consumer`**。

## 2. 三套 `client-type`，各有各的值域

| 出处 | 取值 | 语义 | 谁在读 |
|---|---|---|---|
| **公司网关** `ApiProxyHeadFilter`（`cloud-gateway`） | `ma` / `h5` / `app` | 转发代理时用来判断"要不要从 Redis 补注入 `user-id`/`tenant-id` 头"；**不在这三个里的值直接放行、不做任何注入** | 公司网关 |
| **前端实际发的**（ulink H5 bundle 实测） | `H5` / `H5-WX` | 端形态标识（是否微信浏览器） | 目前只被公司网关的"不匹配就放行"分支吃掉 |
| **AI-Ops** `faq.py` 的 `operator_client_types` | `admin` / `tenant-app` / `MA` / `supply-admin` | 从 UPMS `sys_role.client_type` 读出的**角色维度**：具备这些角色才算"有管家端角色" | AI-Ops 的**平台身份判定** |

> 注意 AI-Ops 那一组来自 **`sys_role.client_type`（UPMS 角色表）**，与网关/前端的
> **请求头 `client-type`** 只是**同名，不同源**。前者是"这个账号是不是管家端角色"，
> 后者是"这次请求从哪种端形态发出"。

## 3. 它们当前是断开的（这是最容易踩的坑）

实测三条：

1. **41 的 Nginx 完全不读 `client-type`**，只读 `X-Business-Entry`（§4）。
2. **ulink H5 前端 bundle 里没有 `X-Business-Entry`**（全量搜索 0 命中），
   它发的是 `client-type: H5` / `H5-WX`。
3. `H5` / `H5-WX` **既不在**公司网关认的 `ma`/`h5`/`app` 里，**也不在** AI-Ops
   认的 `admin`/`tenant-app`/`MA`/`supply-admin` 里 —— 两边都落进"未知值"。

**因此：把 `client-type` 改成"用来区分管家端/客户端"，在当前链路上不会生效。**
要让 `client-type` 承担这个职责，需要同时动三处（网关白名单、Nginx 判据、AI-Ops 取值表），
且三处必须对齐 —— 那是一次**契约变更**，不是接一个头就完的。

## 4. 41 上入口头的实际兜底逻辑

出处：`/www/server/panel/vhost/rewrite/api.mall.qushiyun.com.conf` 的 `location ^~ /v1/`

```nginx
set $aiops_business_entry $http_x_business_entry;
if ($aiops_business_entry = "") { set $aiops_business_entry consumer; }
proxy_set_header X-Business-Entry $aiops_business_entry;
```

**含义**：调用方发了就用它的，没发就**当作 `consumer`**。

**两点值得知道**：

- 兜底方向是**安全**的：`consumer` 是两套里**更窄**的那套（本人范围），
  不会因为漏传头而放大可见范围。
- 但反过来也成立：**入口头是调用方自报的**。谁能发请求、谁就能自称 `operator`。
  这**不是本次改动引入的**，且它不放大到"无范围"——管家端范围仍受该账号所属
  运营商的站点集合约束。真实前端本来就该按入口发这个头。

---

## 附：这篇文档出自哪次排查

2026-09-28 做「管家端入口动作发布 + 订单运营商级授权」的收尾时，需要回答
"前端怎么让请求走管家端那套规则"。当时的假设是"标准版用 `client-type` 区分，
照抄即可"。实际逆向前端 bundle 与公司网关源码后发现三者取值互不相交（§2/§3），
于是把 `X-Business-Entry` 作为唯一判据写进交接文档
（[frontend-operator-handoff.md](frontend-operator-handoff.md)）。
