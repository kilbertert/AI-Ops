## 2026-10-25 · Dify 登录状态维持不了很久：一行配置

**要解决的问题**：运营反馈「每次登录了 Dify 之后登录状态维持不了很久就需要重新登录」。

**根因不是"没有记住设备"，也不是 cookie 没续期。** 查下来是登录代理的一行配置：

```
AIOPS_LOGIN_PROXY_ACCESS_MINUTES=60
```

它进 `mint_console_session()` 后被用作两处：JWT 的 `exp`，和 cookie 的 `Max-Age`。
两者一致，**一小时到点就失效，且没有任何刷新路径能把它续上** ——
`refresh_token` 与 `access_token` 同值（自签模型里"刷新"就是再签一枚），而代理
**没有刷新端点**；Dify 的刷新请求会打到上游，那不是我们的令牌。

所以这个数**就是**运营两次登录之间能待多久。

**做法**：生产改为 `43200`（30 天）。并在 `DifyConsoleSettings.__post_init__` 加范围
校验（1 分钟 ~ 1 年），理由与 `environment` 那条判据相同：**一个配置错误必须在启动
时报，不能表现成"登录服务坏了"**。

上界存在是因为这枚 cookie 是**永久凭据**而不是「记住我」的开关：拿到它就拿到该账号
（`editor`，能编辑和发布智能体）。可以长，但不能没有尽头。

**为什么没做「记住这台设备」**：那需要设备表 + 一条绕过公司登录的判据，等于**放宽认证**。
而实际发生的是"会话到期"，不是"这台机器不被认识"—— 用设备白名单解决到期间题，会把一个
配置值换成一个永久的信任边界。ADR-0136 记了这个否决理由。

**真机验证**（生产 36，Playwright）：

```
cookie __Host-access_token: 剩余 30.0 天 (httpOnly=True, secure=True)
cookie __Host-refresh_token: 剩余 30.0 天 (httpOnly=True, secure=True)
JWT exp 剩余: 30.0 天
刷新后仍在登录态: True
```

**测试**：两条新判据——范围校验（做过变异验证：去掉校验就红）；30 天会话真的签得出来
且 cookie 的 `Max-Age` 跟着走。

**撤销方式**：换 `AIOPS_LOGIN_PROXY_DIFY_SECRET_KEY`，所有自签会话立刻失效。

参见 ADR-0136。
