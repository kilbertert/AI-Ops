## 2026-10-25 · 撤旧 workspace 访问权（#888）

**目标/分支**：`docs/legacy-workspace-revoke`。
**动作类型**：服务主机（36）上的 Dify 数据变更 —— 外部可见、不可逆。
**对象**：`aiops-admin-legacy`（`2db44904-f981-42ca-b7e2-6f35cb2afffd`）。

### 一、这间屋子里有什么（撤权前实测）

| 内容 | 数量 | 说明 |
|---|---|---|
| apps | 2 | `[归档] aiops-41-kefu-agent`（`8e36f0a6`）、`[归档] aiops-41-ops-methodology-agent`（`e35f8fda`） |
| datasets | 1 | `云快充平台协议V1.6.pdf`（`7d13e6d0`，`provider=vendor`） |
| documents | 1 | 47,180 字 |
| segments | **1,564** | |
| **embeddings** | **0** | |
| `documents.indexing_status` | **error** | |
| 成员 | 2 | `AI-Ops Admin`(owner)、`system`(editor) |

**这个知识库从未可用**：`embeddings` 表 0 行、documents 处于 `error`（`VECTOR_STORE` 未配置）。
1,564 个切片一条也检索不到。它们不是"待迁移的数据"，是失败索引的残留。

### 二、为什么这一步可做（#888 的门槛）

票面把本步定义为**唯一不可逆**的一步，并给了明确门槛：

| 门槛 | 状态 |
|---|---|
| 新 workspace 上真的跑过发布与试运行 | ✅ #887：`publish → 200 unchanged v16`；`trial → 202` 直到 `completed`（PR #940）；2026-10-25 `ulink` 真实登录验收 |
| 撤权前 app/dataset 的 maintainer 不是被撤对象 | ✅ 全部 6 个 app 与 5 个 dataset 的 `maintainer` 都是 `AI-Ops Admin`（`a53ad893`），`system`（`8b218530`）一个也没有。所以移除 `system` **在结构上不可能触发归属转移** |
| 恢复路径可用 | ✅ 见下 |

### 三、做了什么

只有一件事：把 `system` 从 `aiops-admin-legacy` 移出。

```
DELETE /console/api/workspaces/current/members/8b218530-fd08-487a-9c83-6859e1f64674
  Authorization: Bearer <ADMIN_API_KEY>
  X-WORKSPACE-ID: 2db44904-f981-42ca-b7e2-6f35cb2afffd
→ 200 {"result":"success","tenant_id":"2db44904-…"}
```

`ADMIN_API_KEY` 从 api 容器**自己的** env 读，不进 argv、不入日志。
`X-WORKSPACE-ID` 让这次请求以 legacy 的 **owner** 身份执行（`ext_login.py:74-90`）——
没有它，`remove_member_from_tenant` 的 `CannotOperateSelfError` 会拦住以 `system` 身份自删。

**没有做的**：不删 workspace、不动 `AI-Ops Admin`（它是 owner，也是唯一还能通过 admin key
访问这间屋子的身份）、不删任何 app/dataset。

### 四、验证（四条）

**1. 撤权后该人不再在旧 workspace 里**

```
aiops-41-Ulink-… | AI-Ops Admin | owner
aiops-41-Ulink-… | system       | editor
aiops-41-Ulink-… | ulink        | editor
aiops-41-platform| AI-Ops Admin | owner
aiops-41-platform| system       | editor
aiops-admin-legacy | AI-Ops Admin | owner      ← system 已不在
```

注意 `system` 在另外两间里仍是 `editor` —— 这次只撤了 legacy 那一处。

**2. app / dataset 归属未变**

撤权前后都是 `a53ad893`（`AI-Ops Admin`）。6 个 app、5 个 dataset 逐一核对，无一变化。
这不是假设：源码里 `remove_member_from_tenant` 只重写 `maintainer == 被移除者` 的行，
而 `system` 名下 0 行。

**3. `dify_app_registry` 未变**

```
1942105476598861824 | consumer | 88fc20bd-7502-4932-91ee-5f632181cac3 | canary-客服
1942105476598861824 | operator | 137cc417-f068-43c2-9d4a-43ebd254ea01 | ops-methodology-700
```

两行在撤权前后逐字相同。

**4. 恢复路径验证过（往返一次）**

按票面要求，在**退役中的**这间屋子上做，不在生产租户上做：

```
POST /inner/api/enterprise/workspace/member   X-Inner-Api-Key: <inner key>
{workspace_id: 2db44904-…, account_id: 8b218530-…, email: 41-1454348737375174658@…, role: editor}
→ 200 {"message":"enterprise workspace member joined."}
```

join 成功，随后再撤一次恢复目标态。**这条证明了 `RBAC_ENABLED=False` 下"移出后无法重新邀请"
的那个坑有绕法**：member 端点走 `create_tenant_member`，不经 invite/activate。

**路径参数陷阱（实测）**：`/inner/api/enterprise/workspace/member` 用 `X-Inner-Api-Key`，
**不是** `Authorization: Bearer` —— 用 Bearer 会得到 `401 inner_api_unauthorized`。

### 五、后果，明写

- **回滚点变成只读。** 那两个 `[归档]` app 现在只能由 `AI-Ops Admin`（service identity）
  经 admin key 操作，没有人类账号能登进去看它们。这不影响它们存在，只影响"有人点进去看一眼"。
- **代价可接受，因为回滚点已被取代**：新屋子的 `88fc20bd` / `137cc417` 跑过真实发布与试运行
  与真实用户登录；旧屋子的两个副本从未被任何真实用户打开过，其数据集也从未可检索。
- **旧 workspace 仍在**（AC 要求）：只撤成员，不删 workspace。
- **`system` 若将来需要重新进入 legacy**，用上面的 member 端点，不用邀请。

### 六、这间屋子接下来怎么办

它现在只剩：一个 owner（服务身份）、2 个归档 app、1 个从未可用的 vendor 数据集。
"不要它了"意味着**删除 workspace** —— 那是另一张票，而且 Dify 没有删 tenant 的 API
（ADR-0059 记录），只能从数据库侧处理，风险与本次不在一个量级。本记录**不**包含删除。
