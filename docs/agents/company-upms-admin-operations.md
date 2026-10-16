# 操作公司 UPMS 管理面（#777 的方法基准）

> **读者**：任何要**写**公司 UPMS 的人（新增菜单/角色/授权）。
> **状态**：2026-10-16 首次实操定稿（#777 建两个 AI-Ops 权限码）。
> 决定与后果论证在 **ADR-0029**；本页只讲**怎么做、怎么判、怎么回滚**。
>
> ⚠️ 这是**写**公司生产系统的规程。读源码的规矩见 `company-code-recon-procedure.md`
> （那一页明令只读）；本页补上它的空白：怎么写、以及为什么不能顺着直觉写。

---

## 0. 三句话

1. **写只打 `api.mall.qushiyun.com`** —— `admin.qumall.qushiyun.com` 的 `/upms/*` 是
   **120 本机的 dev 网关**，写进去会在生产里凭空消失。
2. **登录用真浏览器**（Playwright），验证码用 `ddddocr` 解；手拼 HTTP 对**存在的账号**
   一律 500，对不存在的账号反而正常 —— 别对着手拼请求考古。
3. **`PUT /role/menu` 是先清后写**，只对**新建的空角色**用；碰已有角色会清掉它全部挂载。

---

## 1. 为什么不能手拼登录请求（踩过，别再踩）

`ValidateCodeGatewayFilter` 在网关里校验验证码，**先于密码**。三态只能靠 `msg` 区分
（HTTP 状态码三个分支都是 **428**，不能用来分辨）：

| 条件 | msg | 含义 |
|---|---|---|
| `code` 为空 | `验证码不能为空` | 少传了 |
| `randomStr` 在 Redis 无 key | `验证码失效` | 这个随机串没取过图（或已过期） |
| key 在但字符不等 | `验证码错误` | 读错了 |

**只有相等才 `delete(key)`** ⇒ 读错码**不消耗**该 key，可以在同一张图上重试。
TTL 实测 ≥183 秒（源码 `CODE_TIME=300`）。**字符大小写敏感**。

实测的坑：手拼 `POST /upms/token/login?username=...&password=...&randomStr=...&code=...`
（`@RequestParam`，参数在 **query string**）—— 对**不存在的账号**返回干净的业务错误
`-1000 用户名或密码错误`，但对**任何存在的账号**（无论口令对错）都返回 **500**（Spring 默认
错误信封），且请求根本没到 UPMS（UPMS 容器日志无对应行）。**同一账号同一口令走真浏览器完全
正常。** 所以：不要把时间花在找「缺了哪个头」上，直接用浏览器。

**两条禁用项**（仓库文档明令禁止，用了等于把登录强度降到比公司后台更弱）：
`X-checkCode: ignore` 头、`client-type: tenant-app`（整个跳过验证码过滤器）。

---

## 2. 用 Playwright 登录 + 调接口

### 2.1 页面事实（都是实测）

- 入口 `https://admin.qumall.qushiyun.com/` → hash 路由 `#/login`。
- **三个输入框都没有 name/id**，只能靠 placeholder：
  `请输入账号` / `请输入密码` / `请输入验证码`。
- **没有提交按钮**（页面唯一可见按钮是「忘记密码？」）⇒ `page.keyboard.press("Enter")`。
- 验证码 `img.src` 形如 `/code?randomStr=<数字>`，`randomStr` **由页面生成**。
  取字节：`page.evaluate(async u => {const r=await fetch(u,{credentials:'include'}); ...}, img.src)`
  —— 同一浏览器上下文，保证与页面那张是同一枚。再交 `ddddocr.DdddOcr(show_ad=False)`。
- 成功后 URL 变 `#/wel/index`；令牌在 **localStorage**（**不在 cookie**）：
  `access_token_Local`、`refresh_token_local`、`userId`、`systemId`。

### 2.2 调业务接口的两条路

- **浏览器内**（自动带会话）：`client-type` 非 `BP-admin` 时被前端强制改成 `admin`；
  `system-id` 取 `localStorage.systemId`（缺省 `"0"`）。
- **浏览器外**（拿到令牌后）：手动带三个头
  `Authorization: Bearer <access_token_Local>`、`client-type: admin`、`system-id: <0>`。
  这条路**可以**打 `api.mall`（见 §3）。

凭据放 `~/.config/aiops-company-admin/upms-admin.env`（0600，两字段 `username`/`password`）：
只读进内存，**不进 argv、不打印、不落盘**。

---

## 3. 🔴 写操作只能打 `api.mall`（本页最贵的一条）

| 域名 | `/upms/*` 实际去处 | 能不能写 |
|---|---|---|
| `admin.qumall.qushiyun.com` | 120 nginx rewrite → `127.0.0.1:9999` = **120 本机 `cloud-gateway.jar --spring.profiles.active=dev`** | ❌ **写了会消失** |
| `api.mall.qushiyun.com` | 生产 `cloud-upms-admin` | ✅ 唯一正确目标 |

**失败形态**：POST 到 admin 域名返回 `{"code":0,"msg":null,"data":null,"ok":true}`，
但生产库**一行都没有**；而且 admin 自己的读接口也查不到（它读的同样是 dev 网关）。
「写成功 + 读回空」会互相印证成「也许是异步或缓存」，把排查引向错误方向 —— 我第一次就
栽在这里，建了三条探针才定位到。

**判据（三步都要做）**：
1. 写只打 `https://api.mall.qushiyun.com`；
2. 写后**用同一个域名**读回：`GET /upms/menu/all/tree?name=<刚建的>`；
3. 再用数据库直连核对一次（41 → `qumall_upms`，只读）。

---

## 4. 对象形状（照抄既有行的形状，不要自创）

**按钮型菜单**（`type=1`，`resource` **留空** —— `resource` 会进 `userInfo.rules` 变成一条
网关路径规则，而我们只要能力判据）：

```json
{"name":"AI-Ops 智能体编辑权","permission":"aiops:agent:edit","type":"1",
 "clientType":"admin","parentId":"1000000","path":"","component":"","resource":"",
 "icon":"","sort":996,"category":0,"status":"N","delFlag":"0","sysemId":"0"}
```

`parentId` 用 `1000000`（「系统」）—— 与既有二级分组同层；`sort` 取靠后值避免插队。

**角色**（`tenantId=-1` 是平台级，与既有「系统管理员」同）：

```json
{"sysemId":"0","tenantId":"-1","roleName":"AI-Ops 智能体编辑","roleCode":"aiops_edit",
 "roleDesc":"AI-Ops 专用（#777）","dsType":0,"dsScope":"","delFlag":"0",
 "roleType":"1","clientType":"admin"}
```

**挂菜单**：`PUT /upms/role/menu` body `{"roleId":"<新角色id>","menuId":"<菜单id>"}`。

---

## 5. 四条**会咬人**的语义

1. **`client_type` 是端形态分区，不是权限等级**。登录代理只发一个 `client-type`，
   `findUserInfo` 按它过滤角色 ⇒ **建码时选哪个 client_type，就决定谁能读到它**。选错 =
   目标人群登录后「没有权限」而界面看不出原因。本仓固定用 `admin`（与登录代理一致）。
2. **`POST /menu` 会自动把新菜单挂到 `role_id=1`（系统管理员）**：
   `SysMenuController.save()` 里 `sysMenu.setRoleId(CommonConstants.ROLE_ID_ADMIN)`。
   这是公司对全部 3500+ 权限码的统一做法。**后果**：`system`、`huqq`（`type=-1` 硬编码平台账号）
   自动获得该权限码 —— 切发布权开关时要知道这一点。
3. **`PUT /role/menu` 是 `clearRights(roleId)` 先清后写**（整体替换）
   ⇒ **绝不能用于已有角色**，会清掉它原有全部挂载。只对新建空角色用。
4. **删除是逻辑删除**（`del_flag=1`，行还在），所有查询都带 `del_flag='0'`
   ⇒ 建了又删的探针行会永久占 id。物理删除要显式决定。

---

## 6. 回滚

```bash
# 先解挂载（否则删除菜单会因下级/引用而失败）
curl -X PUT  $API/upms/role/menu   -d '{"roleId":"<rid>","menuId":""}'   # 清空该角色挂载
curl -X DELETE $API/upms/menu/<menu_id>
curl -X DELETE $API/upms/role/<role_id>
```

⚠️ 清挂载那一步对**新建空角色**是安全的（本就只有 1 条）；**永远不要**对已有角色做这个操作。

---

## 7. 验收判据（不是"接口回了 ok"）

1. 库里查得到：`SELECT id,name,permission,type,client_type,del_flag FROM sys_menu WHERE permission LIKE 'aiops:%'`
   —— 两行 `del_flag='0'`。
2. 挂载正确且**只有**新角色：join `sys_role_menu` 看 `role_id` 集合。
3. **端到端**：用真实公司账号登录后读 `/upms/user/info?client-type=admin`，
   `permissions` 数组里出现这两个码 —— 这正是 `dify_login_company.read_permissions` 读的项。
4. **未越界**：`SELECT COUNT(*) FROM sys_menu WHERE del_flag='0'` 前后只差自己新建的数量；
   已有角色的挂载数不变（除公司自己给 role 1 加的那两条）。
