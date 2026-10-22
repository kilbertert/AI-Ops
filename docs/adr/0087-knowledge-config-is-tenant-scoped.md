# 知识库配置的判据是本租户身份，不是平台级全局权限码

日期：2026-10-22
状态：已采纳
影响版本：`/v1/knowledge/available`、`/v1/knowledge/sources`（GET/PUT/DELETE）；
`gateway_api._require_knowledge_admin`；ADR-0029 在知识库这条路上的适用范围

## 背景

2026-10-22 真机验收：Ulink 的真实运营账号（`ulink`，管家端密码登录）点进知识库页，
页面正常、按钮在，但列表返回 **403 `KNOWLEDGE_FORBIDDEN`**。

查到的形状：

| 端形态 | 角色 | roleCode | tenantId | 权限码 |
|---|---|---|---|---|
| `tenant-app`（管家端） | CPO | `321` | `1942…` | 69，无 aiops |
| `admin`（管理端） | Admin | `ROLE_ADMIN` | `1942…` | 363，无 aiops |

两个角色都是**租户级**。`aiops:agent:edit` 建于 ADR-0029，挂在平台级的
`AI-Ops 智能体编辑` 角色上，从未授给任何租户。

## 决定

知识库四条路由的判据改为「**有本租户身份**」，不再要求 `aiops:agent:edit`。

判据本体不变的部分：`effective_tenant_id` 仍然是唯一的租户来源，四条路由全部按它取数，
跨租户越权在结构上取不到别人的库（`PUT` 的 `tenant_id` 与调用者不一致时 403
`KNOWLEDGE_TENANT_MISMATCH`，由既有测试钉住）。

## 为什么这个码在这里不成立

1. **它防的越权已经由租户隔离防住。** ADR-0029 建它时，Dify 是平台级单工作区
   （ADR-0056），所有人共享一个 workspace、能看全部 app，所以必须有全局门。1:1:1 之后
   那个前提没了：Ulink 的运营进自己租户，本来就看不到别的租户。
2. **它的实际效果是把有业务角色的人挡在外面。** 一个持 `ROLE_ADMIN` 的租户管理员，
   配不了自己租户的知识库 —— 而他能配的正是只有他该配的东西。
3. **平台管理员恰好有这个码**，所以此前用 `system` 账号做的验收全绿，掩盖了这一点。

## 被否的三个方案

1. **给 Ulink 授 `aiops:agent:edit`。** 否掉：它不改判据，只是把一次真机发现变成一次
   配置绕过；下一个租户仍然会遇到同一个 403，而且每个新租户都要有人记得去授一次。
   另外他走管家端时角色是 `tenant-app`，还得先解决端形态归属问题。
2. **判据换成公司原生租户管理员编码 `ROLE_ADMIN`。** 方向对，但**证据不足**：
   `/upms/role/list` 只返回**本租户**的 11 个角色，无法证明 `ROLE_ADMIN` 是各租户通用
   编码。把一条无法证伪的假设写成门，会在某个编码不同的租户上静默挡住运营。
3. **只给读放开、写保留全局码。** 否掉：Ulink 的核心诉求是「建完库回来能勾选」，那一步
   就是写。分开会让页面看起来能用、保存时才 403 —— 比整页 403 更难懂。

## 代价，明写

**同一租户内，任何已登录调用者都能改本租户的知识库绑定。** 这在「一个租户只有一个
管理员」时是正确的粒度（Ulink 当前即如此，用户 2026-10-22 确认暂无其他员工）。

租户里出现普通员工账号时，正确动作是**把判据换成该租户自己的管理角色**（公司原生表达
是 `ROLE_ADMIN`，前提是先证明它跨租户通用），**不是退回全局码** —— 后者会重新引入
「平台管理员才能配自己租户」的错位。

## 回归

- `test_a_tenant_admin_without_any_platform_role_can_read_the_picker`
- `test_a_tenant_admin_without_any_platform_role_can_bind`
- `test_a_caller_without_a_tenant_is_still_refused`（门没消失，只是不再要求全局码）
- `test_one_tenant_cannot_bind_against_another`（跨租户仍拒）

测试的调用者替身**刻意不给任何平台角色**：此前它照抄 `ROLE_AGENT_ADMIN`，于是测试在
一条真实身份永远走不到的分支上全绿 —— 那正是这次 403 被藏住的原因。
反向验证：把判据改回 `EDIT_ROLES`，上述用例立刻红。
