# 85. 运维切租户就发生在 Dify 控制台里，靠成员关系，不靠 fork

日期：2026-10-22
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_tenant_access.py`（新）、
`src/aiops_diagnostics/admin_cli.py`（`grant-tenant-access`）、
`docs/agents/env-41-runbook.md`（§3.1-decies）

## 背景

公司运维需要**在 Dify 控制台里切到某个租户的 workspace**，编辑与运维它的智能体。
在此之前我给出了两个错误判断，都记在这里，因为它们各自代表一种会反复出现的错误：

1. **"Dify 没有 workspace 切换器，必须 fork 前端重建镜像。"**
   实际：`app/components/main-nav/components/workspace-switcher.tsx` 一直存在，唯一的隐藏
   条件是 `if (!isPending && !workspaces) return null`。
   **怎么错的**：在**编译后的 chunk** 里搜组件名 —— Next.js 把名字缩略掉了，搜不到不等于
   不存在。拿到上游源码（tag `1.17.1`）一搜就命中。
2. **"`aiops:agent:edit` 的持有者全是平台账号。"**
   实际：持有 `aiops_edit` 角色的只有 1 人（`type=5`，**已删除**，历史残留）；真正持有该
   权限码的是 `ROLE_ADMIN`（25 人）。**怎么错的**：只看第一页数据就下结论。

## 决策

**切租户这件事发生在 Dify 自己的 UI 里，本仓库只负责"让这个人有资格出现在那个列表里"。**

Dify 的机制是完整的：`WorkspaceSwitcher` 读这个账号的 workspace 列表 → 空则整个组件
返回 `null` → 有则渲染 → 点一下 `POST /workspaces/switch` → 服务端按
`tenant_account_joins` 校验后改 `current`。**缺的只有成员关系。**

所以本仓库提供一个命令，把人 join 进去，到此为止：

```bash
aiops admin grant-tenant-access --tenant <id> --account-id <dify id> --email <dify email>
```

## 三条刻意的边界

**一、不碰 `current`。** 切哪个 workspace 是人在 Dify 里点的。命令替人改会让别的标签页、
别的在飞请求看到状态跳动，而且"这次为什么切换了"在事后无法从人身上找到原因。

**二、不批量开通。** 25 个 `ROLE_ADMIN` 里 18 个半年以上没登录过（`login_count` 与
`update_time` 实测）。全量 join 等于给休眠账号开每一个租户的 workspace 访问权 ——
一个早已离岗的账号会因为一条历史权限码而保留所有租户的编辑面入口。

**三、不 fork Dify。** 前端只有一个隐藏条件，而 fork 的代价是每次升级重放改动 + 每次
构建重建 web 镜像（webpack 3.8 分钟 + `--force-recreate`）。仓库已经为另一处前端改动
付出过这个代价，能不再付就不付。

## 被否掉的替代形态

- **fork 前端加切换器。** 结论是**不需要**：切换器已经存在，我先前"不存在"的判断来自在编译
  产物里搜名字。
- **在登录代理上做"选租户后登录"。** 体验上每次换租户要重新登录一次；而 Dify 自带切换器
  点一下就切。代理那条路只适合"这个人只有一个租户"的场景。
- **批量给所有 `ROLE_ADMIN` 开通所有租户。** 见边界二。
- **替人改 `current`。** 见边界一。

## 后果与代价

- **成员关系会累积。** 每给一个人开一个租户就多一行 `tenant_account_joins`。Dify 没有
  "撤销某个 workspace 访问权"之外的批量清理入口，清理要逐行做。这是按需开通的代价，
  也是不批量的理由。
- **`account-id` 与 `email` 必须逐字匹配 Dify 的账号行**（`Account.id == account_id AND
  Account.email == email`）。两者来自两个 id 空间，写错的表现是 `account not found`——
  一句不指向根因的错。
- **租户用户仍然进不了 Dify**：`aiops:agent:edit` 在 `ROLE_ADMIN` 那条线上。这是**另一
  件事**，不是本 ADR 的边界。
