# 管理面的角色来自公司权限码，而不是一个 UPMS 从未建起来的角色族

日期：2026-10-22
状态：已采纳
影响版本：#926 / #923 的真浏览器验收修复；`company_permissions.py`、
`company_token_auth.py`、`gateway_api.py` 的知识库四条路由与 `/v1/agents`

## 背景

知识库四条路由（`/v1/knowledge/available`、`/sources`、console、PUT/DELETE）与
`/v1/agents`、debug-run 都要求 `caller.roles ∩ {ROLE_AGENT_ADMIN, ROLE_PLATFORM_ADMIN}`。
2026-10-22 用真浏览器 + 真实公司账号在 41 上验收，得到 **403 `KNOWLEDGE_FORBIDDEN`**。

根因不是某一条路由写错了，是**这个判据在真实身份路径上永远不可能成立**：

| 解析器 | `roles` |
|---|---|
| `RedisThirdSessionResolver`（`third_session_auth.py:213`） | `frozenset()` |
| `CompanyTokenCallerResolver`（`company_token_auth.py:275`） | `frozenset()` |

真实请求走第二条（41 上 `X-AIOps-Source-Key` 已配置，operator 入口由公司网关注入）。
于是**只有构造身份**（ADR-0058 的 launcher，直接给了 `ROLE_AGENT_ADMIN`）能过 —— 而那正是
之前几轮验收用的身份。单测也照抄了 `roles={"ROLE_AGENT_ADMIN"}`，因此 3453 条全绿。

这件事在 `docs/开发进度.md` 里**早就是已知缺口**（"UPMS 未建 `ROLE_AGENT_ADMIN` 角色族"），
只是挂在了别的功能下面。知识库四条路由是在那个缺口之上新建的，没有回头看。

## 决定

**管理面的角色由公司权限码推导，而不是等 UPMS 建一个角色族。**

- 判据回到公司已经在用的两个码（ADR-0029 建的两行 `sys_menu`）：
  `aiops:agent:edit` → `ROLE_AGENT_ADMIN`（含 `ROLE_AGENT_VIEWER`）；
  `aiops:agent:publish` → `ROLE_AGENT_PUBLISHER`。
- 读取点是 `GET {AIOPS_UPMS_BASE_URL}/user/info` 的 `data.permissions`，用**调用者自己的
  令牌**（不是服务侧令牌 —— 那个只回答服务身份自己的权限）。
- 缓存按**令牌指纹**，不按用户 id。理由：权限随时可能被撤，按用户缓存会把一次撤权拖到
  缓存过期；按令牌最多拖到该令牌自身的有效期，而那是它本来就有的生命周期。
- **读不到 = 空角色集**，不是放行、也不是 500。未配置 UPMS 时一次上游都不打，行为与改动前
  逐字一致 —— 这样"哪些部署会因此多拿到权限"是一个看配置就能回答的问题。

## 被否的方案

1. **在 UPMS 建 `ROLE_AGENT_ADMIN` 角色族，把角色编码塞进会话**（ADR-0021 最初的方向）。
   否掉的理由是它修不了这条链路：`thirdSession` 的载荷里**没有角色字段**，要改就得动公司侧
   的 tokenEnhancer 或会话写入方 —— 我们改不了，也没有排期。而公司**已经**在发权限码了。
2. **只给知识库四条路由放行**（范围最小）。否掉：同一个页面上会变成「知识库能进、
   智能体不能进」，而这两处的判据本来就是同一个。
3. **给 `system` 这类平台账号硬编码角色**。否掉：那是把「这次验收的账号能用」写成规则，
   下一个账号照样 403。

## 边界与代价

- **权限码是全局的，不区分租户**（ADR-0029 已记录）。拿到 `aiops:agent:edit` 的运营在本租户内
  可编辑，但能进哪几个 RAGFlow 租户仍由 `effective_tenant_id` 决定 —— 这两条是独立的。
- **多一次公司调用**。缓存把稳态摊成「每个令牌 TTL 内一次」；TTL 默认 60 秒，不是按请求缓存。
- **撤权的生效窗口 = 令牌有效期**，不是立即。这与 Dify 控制台会话的窗口是同一个取舍。
- 这条改动**没有**让 `AgentManager` 内部的其它角色判据（如发布）自动跟着改 —— 发布权仍由
  `ROLE_AGENT_PUBLISHER` 单独给，不由 edit 蕴含。

## 为什么单测发现不了

三条原因，前两条是同一个：

1. 所有用例直接构造 `ScopeContext`，而构造时照抄了生产**从未给过**的角色。
2. 假上游（`check_token` 替身）只回答令牌声明，从不返回权限码 —— 权限码通道在测试里不存在。
3. 只有真浏览器 + 真实账号能让"roles 为空"这个事实暴露出来：`TestClient` 请求同样会 403，
   但那几轮验收用的是构造身份，绕过了整条真实路径。

回归因此钉在三处：`company_permissions` 的纯函数与缓存、`company_token_auth` 的
「权限码 → roles」（反向验证：改回 `frozenset()` 立刻红）、以及路由层的 cookie 形状。
