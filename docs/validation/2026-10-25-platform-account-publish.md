## 2026-10-25 · 平台账号发布：哨兵 `-1` 被当成租户 id

> **未完成业务验收。** 这一篇记的是一个链路灯判据缺陷：证据来自桥与服务宿主的真实
> 日志、以及可复现的单元用例。它包含一次**真实用户遇到的故障**（`system` 发布被拒），
> 但不是业务侧验收结论。

### 一、症状

`system`（平台账号）点「发布到 AI-Ops」，运营看到：

> 你所在的租户还没有可用的 Dify 工作区，不能发布。请联系管理员为该租户开户后再试

**这句提示不成立**：36 上三个 workspace 都在，`dify_workspaces` 里 `platform` 那行
`setup_state='ready'`。

### 二、真实链路（36 的桥日志）

```
10:36:30  workspace lookup failed: _WorkspaceLookupFailed: 400
10:36:30  console publish refused: the initiator's tenant has no workspace
```

网关回 **400**，不是 404。原因：

| 环节 | 实际值 |
|---|---|
| 映射表 `tenant_id` | `-1`（公司体系的平台哨兵，不是实体 id） |
| 桥发出的查询 | `GET /v1/dify/workspace?tenant_id=-1` |
| 网关校验 | `_SAFE_ID = ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`，`-1` 不匹配 |
| 结果 | `ValueError` → 400 `DIFY_WORKSPACE_TENANT_INVALID` |
| 桥的翻译 | 所有非 2xx → 「没有工作区」 |

网关侧零条 `console publish refused`，因为请求在参数校验就被拒 —— 这本身就是判据。

### 三、修复

`dify_console_publish.py` 对 `X-AIOps-Initiator-Type == "-1"` 映射到 `platform`
workspace，与登录侧 #882 同一条口径。

### 四、第一版是错的（本地全绿、宿主上挂）

第一版从 `dify_login_service` 导入 `PLATFORM_USER_TYPE`。本地 302 条相关用例全过，
但 `test_console_bridge_deployment` 的**五条**立刻转红：

```
ModuleNotFoundError: No module named 'aiops_diagnostics.dify_login_service'
```

因为桥的部署制品只含五个模块（`build-bundle.sh:13`）。桥是一个被裁剪的运行时 ——
**它的模块边界由部署形态决定，不由包结构决定**。

改为在桥内重写两个常量，并用一条用例把副本钉回出处。

### 五、判据

| 用例 | 变异 |
|---|---|
| 平台账号查 `tenant_id=platform` | 把映射改回 `if False` → 转红 |
| 租户账号仍查自己的 tenant（对照） | — |
| 桥内常量与出处一致 | — |

```
3957 passed, 9 skipped
```

### 六、没修的

桥把**所有**非 2xx 译成「没有工作区」，400 与 404 无法区分。下一次参数类错误仍会被
误读成「没开户」。需要单独的改动。
