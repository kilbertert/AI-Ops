# 管家端路由迁移与恢复（受管操作手册）

适用：某条 `chat_route_bindings` 的 Dify 应用消失（批量删除、控制台 404），
但节点仍要能执行、运营也要重获编辑与发布能力时。

**这份手册只写方法与判据，不写口令、令牌、租户明文。** 凭据一律运行时从受控位置读；
41 的访问方式见 [env-41-runbook.md](../env-41-runbook.md)。

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
| 运营在 Dify 控制台改完，用网关发布 | `POST /v1/dify/publish`（body 只有 `app_id`，服务端密钥门） | 是 |
| 无运营会话的受管操作 | 只能调 `node_publication.publish` + `advance` | **否** |

前两条最终都落到 `_publish_bound_app`，第三条会**跳过 `record_publish_audit`**。
选第三条就必须在回执里写明"本次没有审计行"，并且**不要事后补写**。

`SelfService.publish`（`self_service.py:719`）的前置只有两条：
`expected_draft`（给了才比）与 `current.revision != revision` ⇒ `SETUP_ROUTE_CHANGED`。
**它不比较 `current.app_id`。** 所以"迁移必须绕开标准发布"这个理由不成立。
真正比较 `app_id` 的是 `change_version`（`self_service.py:885`）与
`_managed_app_route`（`gateway_api.py:1192`，供 `/v1/dify/restore`、`/v1/dify/versions`）。

## 4. 执行（受管动作）

```text
node_publication.publish(agent_manager, admin_context(tenant), binding, dsl_source, node_target)
    → SyncOutcome(action, version_no, agent_id)
node_publication.advance(context, outcome, binding, node_target, expected_revision=<迁移前 revision>)
    → 写 chat_route_bindings，revision+1；expected_revision 不符则 NodeRouteConflict
```

`advance` 自己会做：环境一致性检查、`expected_revision` CAS、`store.publish()` 写指针。
不要自己 UPDATE 那张表。

执行后立刻回读三处并记录：

1. `chat_route_bindings` 行（`app_id` / `agent_version_no` / `revision` / `state`）；
2. `dify_app_registry` 对应行（是否同步到新 `app_id`）；
3. `publish_audit_ledger` 是否新增一条（**用脚本路径时这里会是空的，如实记录**）。

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

**任何受管入口都不通**：`change_version` 与 `_managed_app_route` 都要求
`current.app_id == app_id`，而路由此刻指向新 app。只能用迁移前备份人工写回，
且**写回时 revision 必须取 `当前值 + 1`**，不能复用旧号。

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
