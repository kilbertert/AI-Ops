# 139. 控制台准入按「进哪个 workspace」分，不按「持有什么码」分

日期：2026-10-25
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_login_service.py`（`LoginPolicy.may_enter` /
`may_publish`、`login` 的门序）、`src/aiops_diagnostics/dify_login_server.py`
（`_publish_check`）

## 背景：ADR-0056 记的那条代价到了该还的时候

ADR-0056 把「无法按租户授予编辑范围」写成一条代价：`aiops:agent:edit` 是**全局权限码**，
挂在 `tenant_id=-1` 的平台角色上，租户拿不到。当时它有一个明确的前提——**只有一个共享
workspace**，所有人同处一室、互相可见全部 app，所以必须有一道全局门。

那个前提在 #879–#886 之后不存在了：一个租户一个 workspace，租户的人进去只看得到本租户的
app。**隔离本身已经是那道门。**

但准入判据没跟着改。实测（36 的 `dify_login_audit`）：

```
2026-10-25T03:13  denied  MISSING_EDIT_PERMISSION
2026-10-25T02:41  denied  MISSING_EDIT_PERMISSION
2026-10-23T02:23  denied  MISSING_EDIT_PERMISSION
```

`ulink`（租户 1942 的主账号，`sys_user.type='1'`）430 条权限码、`aiops:*` 0 条。
它的 workspace `c3f871e1-…` 早就 `ready`，两个 app 也在里面 —— **它进不去自己那一间。**

## 决策

**准入的粒度是「他进哪个 workspace」，不是「他持有什么码」。**

| 进的是哪一间 | 门 | 为什么 |
|---|---|---|
| **租户自己的 workspace** | 他有那个 workspace | 隔离在这一层，他碰不到别人的 app |
| **平台共用 workspace** | 仍然是全局权限码 | 所有平台账号同处一室、互相全部可见，那道门必须还在 |

发布权**与准入同一条边界**：ADR-0073 已经把发布钉死在**发起人所在**的 workspace，
租户账号跨租户在结构上发不出去。

具体到两处：

```python
# 登录
def may_enter(self, permissions, *, tenant_account, has_own_workspace):
    if tenant_account and has_own_workspace:
        return True
    return self.may_edit(permissions)


# 发布（`/__aiops/publish/check`）
def may_publish(self, permissions, *, tenant_account, has_own_workspace):
    if tenant_account and has_own_workspace:
        return True
    return bool(self.publish_permission) and self.publish_permission in permissions
```

## 两处容易写错的地方（都钉了用例与突变）

**一、`has_own_workspace` 必须是「实际解析出来的」，不是「他有个租户」。**

没配那四个键的部署走 ADR-0072 的单一固定工作区，那里解析不出 workspace、隔离不存在。
判据若看「他有租户 id」，一次配置缺省就把整个门拿掉了 —— 门会静默消失，而登录一切正常。
`test_a_single_workspace_deployment_keeps_the_global_gate` 钉住这个方向。

**二、门序：先解析 workspace，再判权。**

反过来的话，一个平台账号在平台 workspace 还没建时会拿到「你缺权限」——把它送到公司管理员
那里，而真正要动的是一条 `aiops admin ensure-workspace --tenant platform`。所以：

```
解析 workspace
├─ 成功 → 判门（租户账号直接进；平台账号要全局码）
└─ 失败 → 租户账号：**这里就报"你的工作区没准备好"**（他的门就是那间屋子）
          平台账号：继续判门，全局码过了再报 workspace
```

两个出口共用 `_refuse_workspace`，因为一处记审计一处没记就会漂。

## 被否掉的替代形态

- **只给 `ulink` 授 `aiops:agent:edit`。** 被否（也是 #887 当时的结论）：它不改判据，只是
  把一次真机发现变成一次配置绕过；下一个租户仍然撞上同一堵墙，而且每个新租户都要有人
  记得去授一次。更要紧的是它给真实客户账号一条**全局**权限 —— 那比"在自己的屋子里编辑"
  大得多。
- **把 `aiops:agent:edit` 改成按租户授予。** 被否：那是改公司 UPMS 的权限模型，需要 ADR
  与安全评审，而且它对"能改哪些 app"依然没有强制力 —— Dify 的门在 workspace，不在权限码。
  多 workspace 之后本来就不需要它了。
- **判据换成公司原生的 `ROLE_ADMIN` 或 `sys_user.type`。** 被否，理由是 ADR-0087 已经写过
  的同一件事：`ROLE_ADMIN` 是否跨租户通用**无法证明**；而 `type` 是"可见范围"的判据
  （`TENANT_LEVEL_USER_TYPES = {-1, 1}`），拿它兼任"编辑权"正是 ADR-0075 警告过的混用。
  只要"他有自己那个 workspace"就够了，不必再借一个语义不同的字段。
- **平台账号也放开（"它能看全部"）。** 被否：`current` 只有一个，跨 workspace 来回切会互相
  踩；而"能改所有租户的智能体"比"能看所有订单"大得多（ADR-0059 §4）。

## 后果与代价

- **租户账号不再需要 `aiops:agent:edit` 才能登控制台。** 这是本记录的全部目的，也是它的
  代价：**该租户内任何账号都能进**（与 ADR-0087 对知识库接受的代价同形）。租户里出现普通
  员工时，正确处理是把门收窄到**该租户自己的管理角色**，而不是退回全局码。
- **平台共用 workspace 的门没动。** `system` / `huqq` 这些账号的行为逐字不变。
- **发布权跟着放宽到同一条边界。** 租户账号在自己 workspace 里既能编辑也能发布；跨租户
  发不出去由 ADR-0073 的解析保证，不由这道门保证。
- **`workspace_joined` 是一次快照。** 发布检查读它，所以窗口是会话寿命（ADR-0136）；
  这与"权限码快照"的窗口本来就是同一个。
- **这次没有真机验收。** 需要 `ulink` 用真实公司账号走一次登录代理，证据是
  `dify_login_audit` 出现 `joined` 而不是 `denied`，以及它能在 Dify 里看到 `c3f871e1-…`
  的两个 app。那是部署后的一步，不在本记录内。
