# 75. 平台账号落到自己的平台 workspace，不进任何租户的工作区

日期：2026-10-21
状态：已采纳（#882 实施；ADR-0059 §4 的边界落成代码）
影响版本：`src/aiops_diagnostics/dify_workspaces.py`（`PLATFORM_TENANT_ID`）、
`src/aiops_diagnostics/dify_login_service.py`（平台分支）

## 背景

ADR-0059 定下"一个租户一个 workspace"，它的**反面约束**是平台账号（公司 `sys_user.type
== -1`）—— 这类账号不属于任何 `sys_organ`，所以：

- 它没有可解析的 `tenant_id`；#883 按 `identity.tenant_id` 解析时，它拿到的要么是空、
  要么是一个不对应任何 workspace 的值；
- 于是多 workspace 一上线，**平台账号登录直接变砖**（"你所在的租户还没有工作区"）。而它
  正是公司身份系统出故障时的应急入口。

这不是一条"顺便也支持一下"的票，它是"每个租户一个 workspace"那条规则的另一半。

## 决策

**一、平台账号解析的是哨兵租户 `platform`。**

`dify_workspaces` 里因此有一行 `(tenant_id='platform', environment=…)`，它的 workspace 名
按同一条派生规则是 `aiops-{environment}-platform`。

**为什么是哨兵而不是空串**：空串在解析路径上本来就被 `_identifier` 判为非法（那是刻意的
—— 空值从来不是"没有"的表达）。平台账号**确实**需要一个自己的 workspace，所以它有一个
明确的、不会被真实 `sys_organ.id` 命中的值。这也让 `ensure-workspace --tenant platform`
直接可用，不需要为它加一条特殊命令。

**二、只认 `type == "-1"`，不认 `"1"`。**

`scope_context.py` 的 `TENANT_LEVEL_USER_TYPES` 是 `{"-1", "1"}`，那是"**可见范围**是
整租户"的判据。这里回答的是另一个问题："它该进哪个 workspace"。

- `type=-1`（平台）：不属于任何租户 ⇒ 进平台 workspace；
- `type=1`（租户主账号）：**属于**它的租户 ⇒ 进那个租户的 workspace。

混用会把租户主账号塞进平台工作区，于是它看不到自己租户的智能体 —— 一个比"登录失败"
更安静的错误。

**三、"可见全部"不通过进别人的 workspace 体现。**

平台账号的跨租户可见性只体现在**试运行的订单范围**与**发布审计**上（ADR-0059 §4）。
编辑面只在自己的平台 workspace：Dify 的 `current` 只有一个，一个账号在多个 workspace
里都是 owner 时来回切会互相踩；而"能改所有租户的智能体"是比"能看所有订单"大得多的权力。

**四、平台 workspace 没建时，拒绝文案指向 `platform`。**

`_workspace_refusal` 拿到的 tenant 是哨兵值，所以错误里给出的命令是
`aiops admin ensure-workspace --tenant platform` —— 运维不会去给一个不存在的租户开户。

## 被否掉的替代形态

- **平台账号进第一个/任意一个租户 workspace。** 那等于给平台账号一个租户的编辑面，
  而它可能根本不是那个租户的人；同时"谁进谁的"变成一次隐式选择。
- **平台账号可以进所有租户 workspace。** 设计明确否过：`current` 只有一个，跨 workspace
  会互相踩，而且这是最大的一次权限放大。
- **用 `tenant_id=""` 表示平台。** 空值在解析路径上被当成非法（有意），把它特别化会让
  "空"同时表示"没有"和"平台"，而那正是本仓反复遇到的"同名不同物"。
- **用 `type=="1"` 一起并入平台。** 见决策二：租户主账号属于它的租户。
- **给平台账号开一条完全独立的解析路径（`resolve_platform_workspace`）。** 它和租户解析
  是同一件事，只是 key 不同；两套解析就是两处会漂的判据。

## 后果与代价

- **`dify_workspaces` 多一行 `platform`**，`ensure-workspace --tenant platform` 是它的
  开户方式；它和租户行走同一条幂等/对账路径。
- **平台 workspace 仍需 #881 才能"能用"**（插件 + 凭据 + 模板 DSL）。本票只保证
  "平台账号有地方可去"，不保证那个地方是满的。
- **没有真机验收**：41 的 `dify_workspaces` 现在有另一个会话建的两行租户映射，但没有
  `platform` 行；"平台账号登录后落到 `aiops-41-platform`"要等那一行建出来。
