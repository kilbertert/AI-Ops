## 2026-10-25 · Dify 控制台准入：租户账号进自己那间屋子

**目标/分支**：`feat/dify-tenant-admission`，基线 `0065b9e`。

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

**验证**：新增 6 条用例 + 4 次突变复验（各让对应用例转红）。全量 `3963 passed, 9 skipped`。
顺带更新 `deploy/m2-live-03/images.lock.json` 的 `source_sha256`（它锁的是构建上下文摘要，
改 `src/` 不改它就红）。`test_consumer_status_default_path` 有一条与本改动无关的红，干净树上
同样红。

**真机验收已完成（2026-10-25 13:07）**：`ulink` 的真实公司账号走 `https://dify.ranlei.work`
完整登录，五条判据全 PASS、跑两遍一致：

- 登录被接受（HTTP 200）；会话可用（`profile.name=ulink`）；
- **只看得见自己那个 workspace** `c3f871e1-…`，平台 workspace `d7d788cf-…` 缺席；
- 看得见本租户三个 app。

审计从 `denied` 变成 `joined` + `provisioned`。**归因可证**：`ulink` 的 363 条权限码里
`aiops:*` 为 0，旧判据下必然被拒（部署前确实被拒 5 次），所以这次能进只能由新判据解释。
详见 `docs/validation/2026-10-25-console-admission.md`。
