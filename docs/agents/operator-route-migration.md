# 管家端路由迁移与恢复（受管操作手册）

适用：某条 `chat_route_bindings` 的 Dify 应用消失（批量删除、控制台 404），
但节点仍要能执行、运营也要重获编辑与发布能力时。

**这份手册只写方法与判据，不写口令、令牌、租户明文。** 凭据一律运行时从受控位置读；
41 的访问方式见 [env-41-runbook.md](env-41-runbook.md)。

## 0. 前提与边界

- 能动手的前提：**目标应用已经存在且已登记**（`operator_apps` 有其 `app_id` 与
  `profile_id`），且它的工具集、模型、节点目标与该入口一致。
  "控制台能打开"不是前提的全部 —— 见 §2 的逐项核对。
- 本条路径**只换执行目标**，不新建应用、不改模型、不改工具、不改知识库授权。
  想顺带改这些，是另一件事，要单独评审。
- 迁移是**生产变更**：走任务分支 + 评审；执行完把关键字段回读写进验证记录。
- **`revision` 只增不减。** 任何"写回旧 revision"的方案都不予采用（§5）。

## 1. 先只读确认现状

```bash
# 健康与线上修订
dev-host exec 41 --allow-service-exec -- curl -sS http://172.18.0.1:8788/health

# 当前路由（只读打开 SQLite；41 上没有 sqlite3 CLI）
dev-host exec 41 --allow-service-exec -- bash -lc \
  '/opt/aiops-41/.venv/bin/python - ' <<'PY'
import sqlite3, json
c = sqlite3.connect("file:/var/lib/aiops-41/gateway/gateway.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT environment,tenant_id,entry,scenario,engine,app_id,agent_id,"
                   "agent_version_no,revision,state,updated_at FROM chat_route_bindings "
                   "ORDER BY entry,tenant_id,scenario"):
    print(json.dumps(dict(r), ensure_ascii=False))
PY
```

要记录下来的字段：`environment` / `tenant_id` / `entry` / `scenario` /
`app_id` / `agent_id` / `agent_version_no` / `revision` / `state` / `updated_at`。

同时确认目标 app 是否在 `operator_apps`（或客户端在受管目录）里带着 `profile_id`。
**没有登记的应用不能作为目标**：迁移器按受管目录解析 profile，缺行就没有节点目标。

## 2. 迁移前逐项核对（缺一项就不迁）

| 项 | 判据 |
|---|---|
| 旧 app 状态 | 控制台 404 才算"消失"；只是报错不算 |
| 新 app 可达 | 控制台 `GET /console/api/apps/<id>` 返回 200 |
| 模型 | 模型名在当前白名单里（管家取 profile 的 `model.name`） |
| 工具 | 应用声明的工具集与节点模板一致（对照 `operator_cpms.TOOL_NAMES` / 模板） |
| 节点清单 | `manifest_file` 可解析，含本入口需要的全部工具 |
| 能力报告 | `require_compatible` 通过（节点上报的版本覆盖清单要求） |
| 目标解析 | `node_publication.target_for(settings, binding)` 返回非空，且 entry/scenario 对得上 |
| **协议差异** | 对比"当前路由服务的版本"与"新 app 会产出的清单"的 `system_messages[0]` |

最后一项最容易漏：**迁移读的是新 app 此刻的正文，不是旧路由冻结的那一份。**
两者协议可能不同版本（实测过一次：890 → 962 字符）。这是行为变更，
要么接受并记录，要么先把协议对齐再迁。**不能写成"结构对等、只差 app_id"。**

```bash
# 对比旧路由服务的版本与新 app 最新版本的协议
dev-host exec 41 --allow-service-exec -- bash -lc '/opt/aiops-41/.venv/bin/python - ' <<'PY'
import sqlite3, json
c = sqlite3.connect("file:/var/lib/aiops-41/gateway/gateway.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
def sysmsg(agent_like, version):
    r = c.execute("SELECT snapshot_json FROM agent_versions WHERE agent_id LIKE ? AND version_no=?",
                  (agent_like, version)).fetchone()
    return json.loads(json.loads(r["snapshot_json"])["node_manifest"])["system_messages"][0]
old = sysmsg("<旧 agent_id 前缀>%", <旧版本号>)
new = sysmsg("<新 agent_id 前缀>%", <新版本号>)
print("old", len(old), "new", len(new), "identical", old == new)
PY
```

## 3. 走哪个发布入口

| 场景 | 入口 | 会写审计账本 |
|---|---|---|
| 运营在页面上改完点「发布到管家」 | `POST /v1/dify/management/publish`（页面按钮背后的接口） | 是 |
| 注册表已指向目标应用后的再次发布 | `POST /v1/dify/publish`（body 只有 `app_id`，服务端密钥门） | 是 |
| 直接调用底层发布器（历史事故路径，不作为常规迁移方法） | `node_publication.publish` + `advance` | **否** |

首次跨应用迁移走管理面：它从 `operator_apps` 授权目标，调用 `SelfService.publish`，
同步注册表和路由并记录审计。`/v1/dify/publish` 走 `_publish_bound_app`，
先查 `dify_app_registry`；注册表仍指旧应用时，新应用会被 `DIFY_APP_NOT_BOUND` 拒绝。
两条接口不是同一个入口，不能互换。缺运营会话时先取得正常授权会话，不降级为无审计发布。

`SelfService.publish`（`self_service.py:719`）在应用授权、知识支持等检查之外的并发条件是：
`expected_draft`（给了才比）与 `current.revision != revision` ⇒ `SETUP_ROUTE_CHANGED`。
**它不比较 `current.app_id`。** 所以"迁移必须绕开标准发布"这个理由不成立。
真正比较 `app_id` 的是 `change_version`（`self_service.py:885`）与
`_managed_app_route`（`gateway_api.py:1192`，供 `/v1/dify/restore`、`/v1/dify/versions`）。

## 4. 执行与回读（受管管理面）

在目标应用的管理面读取当前路由 revision 与草稿摘要，再点「发布到管家」。
页面桥把正常 Dify 会话传到管理面；不要把服务端密钥或会话复制进记录。

```http
POST /v1/dify/management/publish
{"app_id":"<目标应用>","revision":<当前路由 revision>,"draft":"<当前草稿摘要>"}
```

下面仅说明管理面内部调用，**不是可直接复制的迁移脚本**：

```text
node_publication.publish(agent_manager, admin_context(tenant), binding, dsl_source, node_target)
    → SyncOutcome(action, version_no, agent_id)
node_publication.advance(context, outcome, binding, node_target,
                         revision=<迁移前 revision>, register_source=True)
    → 写 chat_route_bindings 与 dify_app_registry；revision 不符则 NodeRouteConflict
context.store.record_publish_audit(...)
    → 管理面记录此次发布身份与版本
```

`advance` 接受的参数名是 `revision`；它在内部传给 `store.publish(expected_revision=...)`
做 CAS。`register_source=True` 使注册表与路由在同一事务更新。底层函数本身不写审计；
管理面负责后续记账。不要自己 UPDATE 路由表。

执行后立刻回读三处并记录：

1. `chat_route_bindings` 行（`app_id` / `agent_version_no` / `revision` / `state`）；
2. `dify_app_registry` 对应行（是否同步到新 `app_id`）；
3. `publish_audit_ledger` 是否新增本次发布身份与版本的记录；缺失就如实记录交付缺口。

## 5. 恢复与回退

### 5.1 回到本应用的历史版本（受管、推荐）

```http
POST /v1/dify/management/version
{"app_id":"<当前 app_id>","revision":<当前 revision>,"version_no":<目标版本>}
```

- `change_version` 从 `agent_versions` 取那一版快照推指针，动作 `restored`，
  **revision +1**，并写审计账本。
- `version_no` 必须是 `POST /v1/dify/management/versions` 返回的、快照 `app_id` 属于本 app 的。
- 传旧 revision 会被 `SETUP_ROUTE_CHANGED` 拒绝（已实测：1 → 2 → 3 单调，旧值被拒）。

### 5.2 退回已删除的旧应用

本手册没有验证过跨应用恢复方法。`change_version` 与 `_managed_app_route` 都要求
`current.app_id == app_id`，所以不能用它们把新应用退回已删除的旧应用。
备份不是直接覆盖数据库的授权，也不是可执行恢复方案；需要另行评审带 CAS、
注册表一致性、权限与审计检查的恢复步骤。本手册不提供人工写表捷径。

为什么不能写回旧 revision：`chat_execution.py:669` 用
`record.route_revision != binding.route_revision` 判定回调配置。把 revision 退回旧值，
会让在旧 revision 签发、尚未完成的任务重新满足这个检查。

写回后编辑器依旧不可用（应用已删），生命周期仍然是断的 —— 这不是"回滚完成"。

### 5.3 备份内容

迁移前保存 `chat_route_bindings` / `dify_app_registry` / `operator_apps` 三张表的相关行，
`0600`，路径写在回执里。备份是回退的唯一依据，**不要只留在 /tmp 就当作永久**。

## 6. 审计缺口怎么记

脚本路径没有痕迹是**既有事实**，正确做法是：

- 在验证记录与状态索引里写明"v24 无 `publish_audit_ledger` 行，最后一条账目是 <时间>"，
  并写清事后说明（谁、何时、做了什么、怎么回读确认的）；
- **不伪造当时审计、不为补账修改生产**；可核查的事后说明 ≠ 当时的流水。
- 下一次同类动作优先走 §3 前两个入口，让账本自己长出来。
