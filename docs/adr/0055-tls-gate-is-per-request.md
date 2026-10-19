# 55. TLS 前置按请求判，不按配置字符串判

日期：2026-10-19
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_login_server.py`（`_arrived_over_tls()`、`_login`
的 TLS 闸门）、`ops/dify-login-proxy/dify-login-proxy.conf`（登录 location 补
`X-Forwarded-Proto`）

## 背景：明文入口照收公司口令

模块 docstring 的第一条硬前置写着：

> **TLS 未就绪则拒绝服务。** 公司密码不得经明文 HTTP 传输。

而它由 `assert_public_origin_is_https` 实现，判据是**配置字符串**是否以 `https://`
开头。那条判据证明的是"我们**有一个** TLS 入口"；它证明不了"**这一次**请求是从这个
入口进来的"。

10008 是 `listen 10008` 的明文入口、公网可达，且与 TLS 入口**共用同一份 nginx 配置**
（这是 #776 / #778 的有意设计：挂一处两个入口同时生效）。于是：

- 2026-10-19 实测：向 `http://36.156.159.175:10008/__aiops/login` POST 一个明文
  `password`，代理照常把它 AES 加密后发往公司 —— **没有任何拒绝**。
- 而且这条路**注定登录不成功**：会话 cookie 恒定 `Secure` + `__Host-`，浏览器在 http
  源上把两者**静默丢弃** ⇒ 登录 200 → 跳转 `/` → 立刻弹回 `/signin`。运营看到的是
  "登录成功但没进去"，而口令已经在明文链路上走过一遍。

**收下口令换不来任何东西，只换来一次明文传输。**

## 决策：`X-Forwarded-Proto == https`，缺失即拒

判据放在请求路径上，`/__aiops/login` 的第一步，早于读取请求体：

```python
if not self._arrived_over_tls():
    → 400 + "此入口是明文 HTTP，不能提交公司口令。请使用 https://dify.ranlei.work 登录"
```

- 取 nginx **覆盖写**的 `X-Forwarded-Proto`（TLS 入口的 `location /` 已设为 `$scheme`）。
- 缺失按非 TLS 处理（fail-closed）：这个进程只监听回环，缺失说明有人直连 10119 ——
  那同样不该收口令。
- nginx 侧的配合**已经在 #848 里落好了**，本票不重复改：`dify-console-10008.conf`
  顶部用 realip 把 `$remote_addr` 修正成真实运营地址，并用
  `map $realip_remote_addr $aiops_upstream_proto` 算出该往上报的协议；登录 snippet 的
  `location` 转发的就是这两个已修正的值。

  ⚠️ **这一票与 #848 是同一段代码的两个侧面，必须一起部署**：只上本票而不上那段 nginx，
  明文与 https 在这一层都是 `$scheme=http` ⇒ 连 https 登录也会被拒。反过来说，
  `X-Forwarded-Proto` **不能**写成 `$scheme`（初版就是这么写的，评审时纠正）：
  这一层是明文监听，`$scheme` 恒为 http，会把 TLS 入口写的 https 覆盖掉。
- 验证码路由**不受影响**：它不含凭据，且登录页在揭示任何错误之前就要先把图显示出来。
  拦住它只会让页面变成一张空白图，把"你走错了入口"这件事实藏起来。

## 被否掉的替代形态

- **在登录 snippet 里写 `proxy_set_header X-Forwarded-Proto $scheme`**（初版写法）。
  这一层是 `listen 10008` 的明文服务，`$scheme` 恒为 `http`，于是 TLS 入口写进来的
  `https` 会被覆盖掉 —— 判据变对的同时，正常登录被自己的闸门挡住。实测见 `docs/validation.md`。
- **只在 nginx 层把 10008 上的 `/__aiops/login` 404 掉**。那是暴露变更（改入口拓扑），
  而 #783 已明确把"退役/关闭 10008"排除在本轮之外；况且判据放在应用里，任何一条新的
  明文暴露都不会悄悄绕过它，而 nginx 配置需要有人记得同步。
- **把 `AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN` 的断言加严**。它本来就在看同一个字符串，
  加严只是换一种写法看同一个东西 —— 而缺陷恰恰是"字符串对了、这次请求不对"。
- **在 10008 上返回 301 跳到 https**。会话 cookie 的问题解决了，但口令仍然是浏览器
  先 POST 到 http 源上的（跳转发生在收到响应之后）。而这台入口的既有定位是"回滚路径"，
  改变它的响应形状不是这一票要动的。
- **按 `Host` 头判**。Host 是可以与 scheme 不一致的（明文入口的 server_name 是 `_`），
  判它会得到"看起来像 https 域名但走的明文"这种组合。

## 后果与代价

- **正向**：口令不再经明文链路离开浏览器；走错入口的运营得到一句告诉他去哪的说明，
  而不是"登录成功又弹回来"。
- **代价一**：多一个必须正确设置的 nginx 头。**漏设的后果是 https 入口也登不进去**
  （fail-closed 的方向），这比漏放行安全，但会是一次显眼的故障 —— 所以它写进了部署
  配置文件的注释里，紧挨着那条 `location`。
- **代价二**：本记录**没有**把 10008 变成"安全的入口"。它仍然公网可达、仍能拿到登录页、
  仍能取验证码。改变的是"它不再接收口令"。
- **未覆盖**：真实浏览器在明文入口上的端到端表现（应当看到那句中文说明）要等部署之后。
