# 0156 — 用户接口规范按方法和路径显式公开

Status: Accepted

## 决策与依据

用户要求测试接口文档可通过公网 OpenAPI 导入。按 OpenAPI 3.1.0 的 Path Item、Schema、
Security Scheme 与 Security Requirement 语义，在现有 `/v1/` 入口提供规范及中文查阅页。
FastAPI `get_openapi(routes=...)` 复用已注册路由和请求模型；明确选择方法/路径，覆盖测试
文档中的23个操作。新增管理路由不会被自动纳入，包括与用户GET同路径的POST。

公网代理把 `third-session` 转为内部会话头，并注入服务端来源认证。因此规范声明C端
apiKey请求头和管家个人HTTP Bearer两种替代方式，保留入口说明，不照抄内部依赖的Header。
响应文档补充统一错误信封和异步任务ID；不改变运行时鉴权或序列化。
媒体签名在URL里，操作级security为空；这不等于媒体资源无需授权。
媒体200/206沿用媒体服务的允许MIME集合；非法Range为416空正文，不套JSON错误信封。
签名拒绝403也为空正文；404可能来自空媒体响应或未配置的JSON错误，不强制为JSON。

参考：[OpenAPI 3.1.0](https://spec.openapis.org/oas/v3.1.0.html)、
[FastAPI扩展规范](https://fastapi.tiangolo.com/how-to/extending-openapi/)。
后者按官方仓库同名文档复核；网页访问受限未用第三方教程替代。

## 备选方案

- 开启FastAPI全量默认文档：最少配置，但混入注册、发布和内部回调，拒绝。
- 手工维护完整静态规范：容易单独交付，但请求约束会与代码双写漂移，拒绝。
- 外部CDN提供Swagger脚本：现成调试UI，但需额外脚本来源与凭据页面边界；本轮交付
  无脚本的本地查阅页与标准JSON，由测试工具执行请求，不新增前端依赖。

## 代价与验证

公开规范需显式添加新用户操作；响应说明仍需随契约变更维护。规范不含运行配置、账号或
真实订单；不导出接口实现的内部docstring。固定现役API服务器地址不采信请求Host头。
已有公网转发可承接，无新增宿主配置、端口或代理；随标准CD部署。
场景和步骤见[验收场景](../acceptance/public-openapi/acceptance.feature)及[QA计划](../acceptance/public-openapi/qa-plan.md)。
