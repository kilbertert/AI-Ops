# 管家端 operator 路由迁移：2b0629cf（已删）→ 137cc417

日期：2026-11-03　环境：41 生产　分支：`fix/operator-route-migration`　PR：#1318

> 本文在 2026-11-03 按独立复核订正过一次，订正点见文末「订正记录」。
> 迁移本身**已在生产执行**，本 PR 只是文档；合并本 PR 不会再次迁移，也不会改任何生产状态。

## 为什么迁

现役 `chat_route_bindings.operator_order_diagnosis` 指向 Dify app
`2b0629cf-bb84-5e1f-bdd8-8b1da2ce9053`，该 app 已于 2026-11-02 01:55 UTC 随批量删除消失
（控制台 404）。节点仍可靠冻结清单执行，但**编辑器 → 发布 → 恢复**这条生命周期已断：
运营在 Dify 里改不到它，也发不出新版本。

指定替代应用：`137cc417-f068-43c2-9d4a-43ebd254ea01`（`aiops-41-ops-methodology-agent`）。

## 迁移前核对（不是直接改一列）

| 项 | 结果 |
|---|---|
| Dify 控制台 200 | `137cc417` 存在；`2b0629cf` 404 |
| 模型在白名单 | `deepseek-v4-1-flash-261017` |
| 工具 | `cpms-node` × 2，与旧版一致 |
| `operator_apps` 登记 | 已有 `137cc417`（`profile_id=operator-41`） |
| 节点清单 | `/etc/aiops-node-runtime/gateway/operator-exec-manifest.json`，工具 `cpms_order_detail` + `cpms_charging_samples` |
| `require_compatible` | OK |
| 目标解析 | entry=operator / scenario=operator_order_diagnosis / agent_name=`ops-methodology-700` |
| 已有发布版本 | agent `agt_727264…` v23，其快照 `app_id` 已是 `137cc417` |

**「两条 agent 结构逐项对等，差别只在 app_id」这句是错的**，已在 2026-11-03 订正。
2026-11-03 从生产库只读回读迁移前现役版本（`agt_5c3e2e…` v1，即 rev16 当时服务的那一版）
与迁移后版本（`agt_727264…` v24）：

| | 迁移前（agt_5c3e2e v1） | 迁移后（agt_727264 v24） |
|---|---|---|
| `system_messages[0]` 长度 | 890 | 962 |
| 含仓库当前 `OPERATOR_OUTPUT` 原文 | **否** | **是** |
| 业务提示词（协议后缀之前那段） | 183 字符 | 183 字符 |
| 工具 | `cpms_order_detail` + `cpms_charging_samples` | 同 |
| 模型 | `deepseek-v4-flash` / `openai-compatible` | 同 |

即：工具、模型、业务提示词相同，但**协议后缀被换成了仓库当前版本**（706 字符 → 778 字符）。
迁移读的是 `137cc417` 此刻的正文，而旧路由冻结在很久以前的 v1，两者协议本来就不是同一份。
所以这次迁移**同时换掉了生效的系统协议**，不只是换 `app_id`。

这不是说换掉就一定错（旧协议本来就是过期的），但它是一条**行为变更**，
必须由知道它的人确认，不能写成"结构对等、只差 app_id"。若要严格只换 `app_id`，
应先把当前协议写成已知版本再迁，本轮没有那样做。

## 走的是哪条路径（订正后）

实际执行方式：以受控脚本直接调用发布器代码。

```
node_publication.publish(mgr, admin, binding, source, target)   # 冻结新版本
node_publication.advance(ctx, outcome, binding, target, revision=16)  # CAS 推进路由
```

`advance` 内部仍做 `expected_revision` 乐观锁、环境一致性、`store.publish()` 写指针
（`node_publication.py:336`），与 UI 发布是同一实现。这一点原记录是对的。

**原记录里"标准 `SelfService.publish()` 要求 `route_for(tenant, entry).app_id == app_id`，
所以迁移必然被挡"这句不成立。** 读源码与临时环境实测都是反证：

- `self_service.py:719` 的 `publish` 只做两处前置：
  `expected_draft`（给了才比）与 `current.revision != revision`（`SETUP_ROUTE_CHANGED`）。
  **没有**比较 `current.app_id`。它拿 `app_id` 去 `authorize_app` 查受管目录，
  目录里有 `137cc417`（`operator_apps` 已登记），这一步会通过。
- 临时数据环境里用 `managed` fixture 复现：先把路由停在一个受管 app 上，
  再用**另一个**受管 app 走 `service.publish(...)`，返回成功且路由移到新 app、
  revision +1。没有抛 `SETUP_ROUTE_CHANGED`。
  （临时用例跑完即删，未进仓库。）

真正被比较 `app_id` 的是另外两处，别混进来：

- `change_version`（`self_service.py:885`）：`current.app_id != app_id` ⇒ `SETUP_ROUTE_CHANGED`。
- `_managed_app_route`（`gateway_api.py:1192`，供 `/v1/dify/restore`、`/v1/dify/versions` 用）：
  `row.app_id != app_id` ⇒ `CHAT_ROUTE_BINDING_MISMATCH`。

**脚本路径真正失去的是审计记账**，不是"绕开一个挡路的守卫"：
`SelfService.publish` 末尾会调 `record_publish_audit`，脚本直接调 `node_publication.*`
就跳过了那一行——已核实 `publish_audit_ledger` 里 `137cc417` 的最大 `version_no` 是 **23**，
v24 没有任何行，最后一条账目停在 `2026-11-02T14:36:06Z`。

选择脚本的实际理由仍没有证据。本轮不把缺少运营会话假定为原因，也不把它作为跳过审计的依据。

### 正确的做法（今后）

1. 先在页面上由有发布权限的运营走「发布到管家」，或
2. 用管理面 `POST /v1/dify/management/publish`（`app_id` + 当前 `revision` + `draft` 摘要），
   两者都会写审计账本；
3. 缺少运营会话时先完成正常授权登录。底层函数调用仅用于解释历史事件，
   不作为常规发布方法；本次审计缺行继续保留，不补造。

## 执行结果（2026-11-03 只读复核，未重做迁移）

```
迁移前：app_id=2b0629cf  engine=node  agent=agt_5c3e2e…  ver=1   rev=16
publish：action=synced   version_no=24  agent=agt_727264…
迁移后：app_id=137cc417  engine=node  agent=agt_727264…  ver=24  rev=17
```

**当前生产库只读回读**（2026-11-03，`gateway.db` 以只读方式打开）：

| 字段 | 值 |
|---|---|
| `entry` / `scenario` | `operator` / `operator_order_diagnosis` |
| `app_id` | `137cc417-f068-43c2-9d4a-43ebd254ea01` |
| `engine` | `node` |
| `agent_id` / `agent_version_no` | `agt_727264b84e9247d3b9164a6d62a35a99` / `24` |
| `revision` / `state` | `17` / `active` |
| `updated_at` | `2026-11-03T02:50:51Z` |
| `dify_app_registry`（operator 行） | 同一 `app_id`，`updated_at` 同一时刻 |

`agent_versions` 里 v22 / v23 / v24 的 `app_id` 都是 `137cc417`，`workspace_id` 一致，
`kind=node-sop`。**没有重做迁移**，本轮只读。

## 验证

**运行时绑定可加载**（以合成 caller 触发 `select_binding`，只读）：

```
select_binding -> app_id=137cc417  agent=agt_727264  ver=24  rev=17
manifest 可解析: True
```

**真机端到端**（rl-1 USB → NAM-AL00，管家 App 正常登录）：

- 首页 → View all → 其他工具 → AI客服 → Order Diagnosis → 选真实订单（订单号不入库）
- `qa_e6bf8ddd…` **completed**
- `agent_runs`：`run_8LWRWa3…` **succeeded**
- `chat_binding=operator_order_diagnosis:production-41:…`
- **`route_revision=17`** ← 走的就是迁移后的新路由
- 正文返回真实订单字段（Prepaid / Electricity fee / Service fee / stop reason 等）

**这条真机证据的边界**：它证明"新路由能跑通一次真实诊断"，**不证明**它跑的是迁移前
一致的系统协议（见上节：协议已经变了），也**不证明** Dify 侧的编辑/发布闭环。

## 回滚

**原记录的回滚方式不可用，已撤下。** 原写法是"把 `chat_route_bindings` 的
`operator_order_diagnosis` 行恢复为 `revision=16` 的旧状态"。两个问题：

1. **revision 必须单调递增。** `ChatRouteBindingStore.publish` 的写语句带
   `revision = ?` 谓词并写 `actual + 1`（`chat_route_bindings.py:220`），
   把 17 写回 16 会让在 rev16 签发、尚未完成的、带 `route_revision=16` 的任务
   重新满足 `chat_execution.py:669` 的回调版本检查。
2. **写回已删应用这条路，任何受管入口都不通。** `change_version` 与 `_managed_app_route`
   都要求 `current.app_id == app_id`：路由现在指向 `137cc417`，拿 `2b0629cf` 去调
   任何一个都会被 409 拒绝。所以"通过恢复接口退回旧应用"不是一个存在的选项。

已验证的恢复与仍未验证的跨应用回退分别如下：

**A. 回到本应用的历史版本（推荐，全程受管）**

```
POST /v1/dify/management/version
{"app_id":"137cc417-…","revision":<当前 revision>,"version_no":<目标版本>}
```

`change_version` 从 `agent_versions` 取那一版快照推指针，动作为 `restored`，
**产生 revision+1**，并写审计账本。`version_no` 必须是 `versions` 返回的、且快照
`app_id` 属于本 app 的那些。

临时数据环境已核实该语义（用 `managed` fixture，未进仓库）：

```
after create: revision 1
after retract: retracted revision 2
after restore: {'revision': 3, 'state': 'active', 'version_no': 1} -> revision 3
stale revision refused: SETUP_ROUTE_CHANGED
```

即 1 → 2 → 3 单调递增，拿旧 revision 再恢复会被拒。

**B. 退回已删除的旧应用（未验证，不提供执行步骤）**

- 迁移前备份保留为取证与恢复设计输入；它不是直接覆盖三张表的授权。
- 需要另行设计带 CAS、注册表一致性、权限和审计核验的恢复步骤，不能仅靠 revision+1
  就认定人工写表安全。本轮没有验证这种跨应用回退。
- 写回后编辑器依旧不可用（应用已删），生命周期仍然是断的。
- 这条不是"回滚完成"，是"回到一个同样有问题、但节点还能跑的旧状态"。
- 本轮**不执行**、不建议；只有在 A 也不可用时才考虑，并且要另行评审。

## 未覆盖（如实保留）

- **Dify 侧的编辑/发布闭环未验证**：只把路由接到现存 app。运营在控制台改该 app 并点发布、
  以及"发布后新会话生效"，属端到端生命周期的另一段，未在本轮做。
- **审计账本缺一条**：脚本路径跳过了 `record_publish_audit`，`publish_audit_ledger`
  无 v24 记录（已核实：该 app 最大 `version_no=23`，最后一条账目 `2026-11-02T14:36:06Z`）。
  **这是真实缺口，不补写、不伪造当时审计、不为补账修改生产。** 事后说明只写在本文与
  状态索引里，可核查但不冒充当时的流水。
- **生效的系统协议被同时换掉**（890 → 962 字符，换成仓库当前 `OPERATOR_OUTPUT`），
  见上文第一节。是否可接受由业务决定，本轮只记录。
- 另一租户（`1941847786056323072`）的 operator 路由未动。

## 订正记录（2026-11-03）

| 原写法 | 订正后 | 依据 |
|---|---|---|
| 「差别只在 app_id」 | 工具/模型/业务提示词相同，**协议后缀已换** | 生产库只读回读 v1 与 v24 的 `system_messages[0]` |
| 「`SelfService.publish()` 要求路由已指向本 app，所以必须绕行」 | **不成立**：它只比 revision；真正失去的是审计记账 | `self_service.py:719-734`；临时环境复现 |
| 回滚「把 revision 写回 16」 | 必须走受管恢复产生 revision+1；写回旧 revision 会重新开放回调 | `chat_route_bindings.py:220`、`chat_execution.py:669`；临时环境复现 1→2→3 |
| 回滚「恢复 `chat_route_bindings` 旧行」 | 退回已删应用**任何受管入口都不通**；退回本应用历史版本才受支持 | `self_service.py:885`、`gateway_api.py:1192` |
