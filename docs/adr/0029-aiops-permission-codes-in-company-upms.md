# AI-Ops 的两个权限码住在公司 UPMS 里，跟随公司的菜单→角色模型

日期：2026-10-16
状态：已采纳
影响版本：PRD #772 的身份闭环（子票 #777）；`AIOPS_LOGIN_PROXY_*_PERMISSION`、
`ops/dify-login-proxy/`

## 背景

登录代理要回答两个问题：**谁能进 Dify 控制台**、**谁能把改动发布到 AI-Ops**。判据是公司
权限码（PRD #772 A 已定），但这两个码在公司系统里**不存在** —— 实测 `sys_menu` 里
`permission LIKE 'aiops%'` 0 行、`sys_role` 里 aiops 角色 0 行。

本记录定的是**它们以什么形态存在**，以及三处只能由我们决定、且后果会外溢到别人的选择。

## 决定

**两个权限码就是 `sys_menu` 的两行 + `sys_role` 的两个空角色**，完全跟随公司自己的模型，
不新造机制、不改任何公司代码。

实测的权威定义（`SysUserServiceImpl.findUserInfo`，`cloud-upms:release`）：

```
角色集 = findRoleListByUserId(userId, client-type 头)   ← 按 sys_role.client_type 过滤
       + 硬编码账号自动获得 role_id=1（全量菜单）
权限码集 = Σ（该角色经 sys_role_menu 挂到的每个 sys_menu.permission）+ 角色的 role_code
```

⇒ **权限码不是"挂给人"的**，它是 `菜单(权限码) → sys_role_menu → 角色 → sys_user_role → 用户`
这条链的末端。这一条值得写下来，因为它纠正了一个自然的误解：问"这两个码挂给哪个角色"，
就像问"`sys_menu` 这一行属于谁"—— 它谁也不属于，**授予发生在角色那一层**。

已建的对象（2026-10-16，公司生产）：

| 对象 | id |
|---|---|
| 菜单 `aiops:agent:edit` | `2110974479152324609` |
| 菜单 `aiops:agent:publish` | `2110974481115258881` |
| 角色 `AI-Ops 智能体编辑`（`aiops_edit`） | `2110974759717707777` |
| 角色 `AI-Ops 智能体发布`（`aiops_publish`） | `2110974760334270465` |

两个角色各只挂一个菜单，**不挂任何菜单页面** —— 它们是纯权限判据，不是给人点的入口。

### 为什么 `client_type = admin`

`client_type` 是**端形态分区**，不是权限等级。实测分布：

| client_type | 角色数 | 菜单数 |
|---|---|---|
| `admin` | 1290 | 3535 |
| `tenant-app` | 321 | 487 |
| `supply-admin` | 1 | 929 |
| `MA` | 2 | — |

登录代理只发**一个** `client-type` 头去问 `/upms/user/info`，而 `findUserInfo` 按它过滤角色
⇒ **建码时选哪个 `client_type`，就决定了哪些角色的持有者能读到它**。选错的表现是目标人群
登录后「没有权限」，而界面看不出原因。

选 `admin` 的理由不是"它更高级"，而是：**登录代理已经在用 `admin` 读取**
（`AIOPS_LOGIN_PROXY_COMPANY_CLIENT_TYPE`），而且改它会连带改变"谁能进 Dify"这个业务结论。
两者必须一致，否则建了码也没人读得到。

### 为什么建两个专用空角色，而不是挂到已有角色

`PUT /role/menu` 的实现是 `clearRights(roleId)` **先清后写**，传的 `menuIds` 是**整体替换**
（`SysRoleMenuServiceImpl.saveRoleMenus`）。⇒ 拿它去改一个已有角色会**清掉那个角色原有的全部挂载**。

所以：**只对新建的空角色用**。两个新角色因此各自只有 1 条挂载，而任何现有角色一行未动
（role 1 的挂载数 2661 → 2663，那 +2 是公司自己给系统管理员加的，见下）。

## 三处必须写下来的后果

### 1. `POST /menu` 会自动把新菜单挂到系统管理员（这是公司的统一做法，不是我们的选择）

`SysMenuController.save()` 里：

```java
@PreAuthorize("@ato.hasAuthority('sys:menu:add')")
public R save(@Valid @RequestBody SysMenu sysMenu) {
    sysMenu.setRoleId(CommonConstants.ROLE_ID_ADMIN);   // ← 硬编码 role_id=1，SYS_ROLE_ID="1"
```

于是 `role_id=1`（系统管理员）**自动获得**这两个权限码，而两个硬编码平台账号
（`system`、`huqq`，`type=-1`）自动持有 role 1 ⇒ **它们现在持有 `aiops:agent:publish`**。

**这不是缺陷，是公司对其全部 3500+ 权限码的一贯做法**，我们不去改它。但它有一个必须
被知道的外溢后果：**cutover 打开 `AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL` 的那一刻，
这两个平台账号就能发布到 AI-Ops。**

### 这个后果被**显式接受**（用户 2026-10-16 拍板），理由要说清楚

按 `findUserInfo` 的 SQL 语义实测：`system` 与 `huqq` 通过 role 1 带出
`['aiops:agent:edit', 'aiops:agent:publish']`。**确实持有。**

曾考虑"把 role 1 从这两个账号摘掉"，核对源码后**否掉**：登录路径里有一道

```java
if (ArrayUtil.isEmpty(userInfo.getRoles()) || ArrayUtil.isEmpty(userInfo.getPermissions())) {
    return R.failed("账号没有权限，请找管理员先配置权限！");
}
```

而这两个账号的权限**全部来自 role 1**（`sys_user_role` 里各只有那一行）⇒ 摘掉之后它们
`permissions` 为空、**再也登不进 UPMS 后台**，而它们是应急通道。
**「让系统管理员不持有发布权」与「保留系统管理员能登录」在这个模型里互斥** ——
要同时成立只能改公司代码，而那是外部治理资产。

因此选择接受，并记下**为什么这个接受是低风险的**：

1. **发布路径上的发起人不是 UPMS 登录**，而是「Dify 会话 → 代理映射表 → 公司用户」。
   在这两个平台账号被**显式映射**到 Dify 账号之前，持有权限码没有任何路径能用到它。
2. 它们本就是公司的最高权限账号（`type=-1`、`tenant_id=-1`、硬编码全量菜单）。
3. 代价是**审计可见**的：若它们真的发布了，`publish_audit_ledger` 里记的就是它们，
   不是无声的。

**收紧的动作仍然只有一处**：切开关前把这两个账号从 role 1 摘掉（代价见上），
或改用别的判据。这是一个可逆的、单点的选择。

### 2. 写操作只能打 `api.mall`，**不能**打 `admin.qumall`

`admin.qumall.qushiyun.com` 的 `/upms/*` 被 120 的 nginx rewrite 转到 `127.0.0.1:9999`
—— 那是 **120 上的 `cloud-gateway.jar --spring.profiles.active=dev`（本机 dev 网关）**。
而 AI-Ops 读的是 `api.mall.qushiyun.com` → 生产 `cloud-upms-admin`。

失败形态很危险：POST 到 admin 域名返回 `{"code":0,...,"ok":true}`，**但生产库里一行都没有**；
连 admin 自己的读接口也查不到（它读的也是 dev 网关）。「写成功 + 读回空」两个信号会互相
印证成「也许是异步或缓存」，把排查引向错误方向。**判据：写只打 `api.mall`，写后立刻用
同一个域名读回，再用数据库直连核对一次。**

（登录仍然可以走 admin 域名 —— 那是给人用的界面；只有**写**必须打到生产面。）

### 3. 删除是逻辑删除

`removeMenuById` 只把 `del_flag` 置 1，行仍在，而所有查询都带 `del_flag='0'`。排障期间建的
探针行因此会永久占着 id。物理删除是绕过公司语义的操作，需要显式决定（见「后果」末条）。

## 被否掉的替代形态

- **改公司代码，加一个"AI-Ops 专用"的权限表。** 那是让一个外部治理资产为我们的方便而分叉，
  而现有模型已经能表达这件事（权限码 = 菜单的一行）。**否掉。**
- **用 `role_code` 代替权限码**（`findUserInfo` 也把 `role_code` 并进权限集）。那会把
  "能不能进 Dify"绑到一个**角色**上，而角色是给人分组用的、会被人调整；权限码是稳定标识。
  **否掉。**
- **挂到已有角色**（例如给 `Agent` 角色加一条）。省事，但 `PUT /role/menu` 是先清后写，
  一次误操作清掉那个角色 190 条挂载。**否掉。**
- **用 `client-type: tenant-app` 或 `X-checkCode: ignore` 简化登录。** 两者都会把登录强度
  降到比公司后台更弱（前者整个跳过验证码过滤器）。**否掉**，宁可自动识别验证码。
- **建菜单时把 `resource` 也填上。** `resource` 会进 `userInfo.rules`（网关路径规则），
  而我们只要一个能力判据，不要一条路由规则。**否掉** —— `resource` 留空。

## 后果

- 登录代理的 `AIOPS_LOGIN_PROXY_EDIT_PERMISSION` / `_PUBLISH_PERMISSION` 现在有生产值了，
  与 `ops/dify-login-proxy/login-proxy.env.example` 里的默认值逐字一致（那两个值从一开始
  就写对了，只是一直没有对应物）。
- **两个码对任何人都还没生效**：映射是"角色挂菜单"，而两个专用角色**没有任何用户**。
  要授予谁，就在公司后台把该角色给那个人 —— 这是一次**显式**的授权动作。
- **两条软删除的探针行**（`2110974074435543041`、`2110974170397024257`，名字 `AI-Ops probe`，
  `del_flag=1`）留在库里。**决定保留，不物删**（用户 2026-10-16 确认）：它们
  `sys_role_menu` 挂载为 0、不被任何表引用、也不出现在任何查询里（都带 `del_flag='0'`）——
  是孤儿行而不是坏数据。而物删要绕过两件事：`removeMenuById` 的逻辑删除是公司**刻意**的语义；
  且删除路径会跑 `menuSyncService.sync()`（可能跨库）。为一个纯视觉的收益去发一条
  未走应用逻辑的生产写语句，风险不对等。
- 端到端验证（不是"接口回了 ok"）：用真实公司账号登录后读
  `/upms/user/info?client-type=admin`，`permissions` 里出现这两个码 —— 正是
  `dify_login_company.read_permissions` 读的那一项。
