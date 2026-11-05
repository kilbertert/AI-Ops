# 231. 能力按来源分档：required_scope 只比对不回填，UPMS 任意权限门退役

日期：2026-11-05。

Status: Accepted（issue #1040，方案 A；用户确认后实施）。

「一个调用者能不能做这件事」在同名字段上有四种渲染：会话链与公司令牌链
把路由请求的 `required_scope` **原样写回** `context.permissions`——凭空的
`totally:invented:scope` 也被「授予」；UPMS 链只判「有没有任意权限」不判
「是不是这个」；只有自省链做精确成员判定。`permissions` 因此有两种语义：
一条链上是断言，另外三条上是回声。它不进范围指纹、不参与可见性，只进审计
——读起来像「这个人被授予了 X」，实际是「路由问他要了 X」。

决定：能力是一个**有来源档**的概念，`capability.py` 一张判定表驱动：

- `ASSERTED`——凭据自身断言 scope 集合，`capability_verdict` 做唯一成员
  判定，缺 `required_scope` 即拒（自省链）。
- `DERIVED`——能力由目录推导（公司权限码→角色，ADR-0084），请求的 scope
  不参与授权；`permissions` 记空集（会话链、公司令牌链）。
- `UNASSERTED`——链只判身份与租户，cloud-auth 的平台权限码不是资源 scope；
  `permissions` 记空集，需要能力的面由角色门兜底（UPMS 链）。

配套：

- `ScopeContext` 增 `capability_source`（枚举校验，随 `scope_json` 持久化），
  **不进指纹**——档位不改变任何可见性判定。早于本字段的历史行读回
  `UNASSERTED`，字段内容原样保留，不改写历史。
- UPMS 链的 `if not context.permissions → 403` 删除：那是「有任意平台权限
  就放行」的错配——同一份权限集与自省链给相反答案。空权限集调用者现在
  能过身份层，管理面 403 由角色门产生（角色本就是今天唯一被执行的维度）。
- 路由的 `required_scope` 声明逐字保留；对外状态码、错误码、响应形状
  零改动——`INSUFFICIENT_SCOPE` 映射不变，变的是只有 ASSERTED 链能产生它。
- 语法级守卫：`src/` 内 `permissions=frozenset({required_scope})` 即红；
  测试替身允许这个形状但必须同调用声明 `capability_source`（替身冒充的
  就是「问什么有什么」，它得说自己是谁）。

## 备选方案

- 所有链真执行 `required_scope`：41 的公司令牌 `scope` 逐字是 `["server"]`、
  会话链根本没有 scope 字段——等于用全员 403 换一句准确，且与 ADR-0009
  （身份取令牌、能力不来自令牌）冲突，否决。
- 只改文档保留回填：`permissions` 继续在审计里冒充已核验授权，下一条带
  `AGENT_MANAGE_SCOPE` 的新路由继承同一个错觉，否决。
- 从 `ScopeContext` 删 `permissions`：`scope_json` 是已落库形状，删字段波及
  重放与历史行；先把它变成「有来源、可为空」，独立迁移另行评估，否决。
