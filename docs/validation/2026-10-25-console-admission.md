## 2026-10-25 · Dify 控制台准入：租户账号进自己那间屋子，不要全局权限码

**目标/分支**：`feat/dify-tenant-admission`，基线 `0065b9e`。

### 一、查的是什么

用户要求做 ADR-0056 的后续（多 workspace 隔离）。核实后发现**代码部分早就做完了**：

| 项 | 状态 |
|---|---|
| workspace 表 / 解析函数 / 拒绝码（#879） | 已合并 |
| 建 workspace 的幂等与对账（#880） | 已合并，36 上三个 workspace `ready` |
| 平台账号落自己的 workspace（#882） | 已合并 |
| 发布 / 试运行 / 运维 CLI 按租户解析（#884–#886） | 已合并 |
| 第一个租户迁进自己的 workspace（#887 前五步） | 已迁：`c3f871e1-…` 里两个 app，旧 workspace 保留 |

**但隔离从没被真实租户的人走过一次。** 36 的 `dify_login_audit` 里 `ulink`
（租户 1942 主账号，`type=1`）被拒 **5 次**，最近一次 2026-10-25T03:13：

```
denied  MISSING_EDIT_PERMISSION
```

原因就是 ADR-0056 那条代价：`AIOPS_LOGIN_PROXY_EDIT_PERMISSION=aiops:agent:edit` 是全局码，
挂在 `tenant_id=-1` 的平台角色上；`ulink` 430 条权限码里 `aiops:*` 0 条。

### 二、改法

准入的粒度从「持有什么码」变成「进哪个 workspace」（ADR-0139）：

- 租户自己的 workspace —— 隔离就是那道门，有那间屋子就能进；
- 平台共用 workspace —— 仍要全局权限码（所有人同处一室、互相全部可见）；
- 发布权与准入同一条边界 —— ADR-0073 已把发布钉死在发起人所在的 workspace。

### 三、判据

新增 6 条用例，4 次突变各让对应用例转红：

| 用例 | 钉住的规则 | 突变（转红） |
|---|---|---|
| `test_a_tenant_account_enters_its_own_workspace_without_the_global_code` | 租户账号没全局码也能进 | `may_enter` 的租户分支改 `if False` |
| `test_a_tenant_account_without_its_workspace_is_still_refused` | 没屋子就拒，文案指向 `ensure-workspace` | 删掉租户的 workspace 拒绝分支 |
| `test_the_platform_workspace_still_requires_the_global_code` | 平台那间门不动 | — |
| `test_a_single_workspace_deployment_keeps_the_global_gate` | 没配四键的部署门逐字不变 | `has_own_workspace` 改常量 `True` |
| `test_a_tenant_account_publishes_in_its_own_workspace_without_the_global_code` | 发布同一边界，且要求真的 join 过 | — |
| `test_a_platform_account_still_needs_the_global_publish_code` | 平台账号仍要发布码 | `tenant_account` 改常量 `True` |

全量：`3963 passed, 9 skipped`。

### 四、顺带的两处

- **`deploy/m2-live-03/images.lock.json` 的 `source_sha256` 必须跟着 `src/` 动。**
  它锁的是构建上下文摘要；改了 `src/` 不改它，`test_m2_live03_deploy` 直接红
  （实测 `3c23b13d…` → `0be5fa04…`）。这是设计好的行为，不是噪音。
  没有跑真实构建（要 docker + 拉基础镜像），只重算了内容摘要——`image_id`
  留原值，它本来就"每次构建都不同、由宿主 identity.json 记录"。
- **`tests/test_consumer_status_default_path.py` 有一条与本改动无关的红**
  （`scenarios` 多出 `node`）。它在干净树上同样红，未动。

### 五、没做的

**真机验收没跑。** 需要 `ulink` 用真实公司账号走一次登录代理，判据是
`dify_login_audit` 出现 `joined` 而不是 `denied`，且它能在 Dify 里看到
`c3f871e1-…` 的两个 app。那是部署后的一步。
