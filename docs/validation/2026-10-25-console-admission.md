## 2026-10-25 · Dify 控制台准入：租户账号进自己那间屋子

**目标/分支**：`feat/dify-tenant-admission`，基线 `0065b9e`（PR #1083 / ADR-0139）。
**部署**：36 上登录代理 `revision` 与 `main` 同哈希；发布桥 `revision=8ad3d31`
（PR #1081）。

用户要求做 ADR-0056 的后续（多 workspace 隔离）。**核实后发现代码部分早已完成**
（#879–#886 全部关闭，36 上三个 workspace `ready`，租户 1942 的两个 app 已在
`c3f871e1-…`）。真正缺的是最后一步：**租户的人进不来**。

36 的 `dify_login_audit` 里 `ulink`（租户 1942 主账号，`type=1`）被拒 5 次，最近一次
2026-10-25T03:13，原因都是 `MISSING_EDIT_PERMISSION` —— `aiops:agent:edit` 是全局码，
挂在 `tenant_id=-1` 的平台角色上，租户拿不到（ADR-0056 记的那条代价）。

**改法**（ADR-0139）：准入粒度从「持有什么码」变成「进哪个 workspace」。

- 租户自己的 workspace ⇒ 隔离就是那道门，有那间屋子就能进；
- 平台共用 workspace ⇒ 仍要全局权限码（所有人同处一室、互相全部可见）；
- 发布权与准入同一条边界（ADR-0073 已把发布钉死在发起人所在的 workspace）。

**两处必须写对的地方**：`has_own_workspace` 看的是**实际解析出来的** workspace（不是
"他有租户"，否则没配四键的部署会静默丢掉整道门）；门序是**先解析 workspace、再判权**
（否则平台账号会拿到"你缺权限"，而真正要动的是 `ensure-workspace`）。

---

### 真机验收（2026-10-25 13:07，`ulink` 真实公司账号）

走**真实 TLS 入口** `https://dify.ranlei.work`：取验证码 → 解 → 用真实公司账号登录，
再拿控制台会话问 Dify「我是谁、能看见谁」。

| # | 判据 | 结果 |
|---|---|---|
| 1 | 公司账号登录被接受 | **PASS** HTTP 200, `ok=true` |
| 2 | 会话可用（能读到自己） | **PASS** HTTP 200, `name=ulink` |
| 3 | 只看得见**自己那个** workspace | **PASS** 列表恰好 `["c3f871e1-ffd0-4a10-a84a-b8696f7acd25"]`（`aiops-41-Ulink-598861824`, `current=true`） |
| 4 | 平台 workspace **不在**列表里 | **PASS** `d7d788cf-…` 缺席 |
| 5 | 看得见本租户的三个 app | **PASS** `aiops-41-kefu-agent` / `aiops-41-ops-methodology-agent` / `aiops-workspace-template` |

跑了两遍，两次都 ALL PASS。

**审计与映射**（36 的 `mappings.db`）：

```
ulink | 1942105476598861824 | type=1 | c3f871e1-… | joined=1 | active
joined   c3f871e1-ffd0-4a10-a84a-b8696f7acd25   2026-10-25T05:07:11Z
provisioned FIRST_LOGIN                          2026-10-25T05:07:11Z
```

登录代理日志：`user=1942105476975915010 account=d351e7d6-… permissions=363`。

### 归因：这次成功**只能**由新判据解释

`ulink` 的权限码快照实测：

```
total perms = 363
aiops:*     = []          ← 0 条
has aiops:agent:edit    = False
has aiops:agent:publish = False
```

旧判据（`aiops:agent:edit in permissions`）下它必然被拒 —— 部署前它确实被拒了 5 次。
所以"能登录"这件事本身没有第二种解释。

### 隔离是 Dify 侧强制，不是我们的判据在拦

第 3、4 条的判据取自 `GET /console/api/workspaces` 的返回，而那个端点按
`tenant_account_joins` 取数。`ulink` 只在 `c3f871e1` 里有成员关系（`workspace_joined=1`
是代理 join 的结果），因此平台 workspace 在**数据结构上**进不来 —— 即使它哪天拿到全局
权限码，也看不见平台那间的 app。

---

**验证**（仓库内）：新增 6 条用例 + 4 次突变复验（各让对应用例转红）。全量
`3963 passed, 9 skipped`。顺带更新 `deploy/m2-live-03/images.lock.json` 的
`source_sha256`（它锁的是构建上下文摘要，改 `src/` 不改它就红）。
`test_consumer_status_default_path` 有一条与本改动无关的红，干净树上同样红。

**密码处理**：真实口令只经 stdin 进脚本变量，不进 argv、不落盘、不进日志，验收脚本
跑完即弃。
