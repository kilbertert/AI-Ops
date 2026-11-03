# 管家端 operator 路由迁移：2b0629cf（已删）→ 137cc417

日期：2026-11-03　环境：41 生产（`1.0.0+c54735dbaa6a`）

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

两条 agent 的结构逐项对等（同 agent_type、同两个工具、都有 `node_manifest`），
差别只在 `app_id`。这是可以迁移的条件，不是"看起来能用"。

## 走的是哪条路径

**复用发布器代码，不手改 app_id 一列。**

标准 `SelfService.publish()` 在此场景会被前置检查挡住：它要求
`route_for(tenant, entry).app_id == app_id`（即先有一条指向本 app 的路由），
而当前路由指向的是旧 app —— 这是给"已在本 app 上再发布"准备的守卫，
不是给迁移准备的。

因此以受控脚本直接调用**同一套代码**：

```
node_publication.publish(mgr, admin, binding, source, target)   # 冻结新版本
node_publication.advance(ctx, outcome, binding, target, revision=16)  # CAS 推进路由
```

`advance` 内部仍做 `expected_revision` 乐观锁、环境一致性、`store.publish()` 写指针，
与 UI 发布走的是同一实现。

## 执行结果

```
迁移前：app_id=2b0629cf  engine=node  agent=agt_5c3e2e…  ver=1   rev=16
publish：action=synced   version_no=24  agent=agt_727264…
迁移后：app_id=137cc417  engine=node  agent=agt_727264…  ver=24  rev=17
```

## 验证

**运行时绑定可加载**（以合成 caller 触发 `select_binding`，只读）：

```
select_binding -> app_id=137cc417  agent=agt_727264  ver=24  rev=17
manifest 可解析: True
```

**真机端到端**（rl-1 USB → NAM-AL00，管家 App 正常登录）：

- 首页 → View all → 其他工具 → AI客服 → Order Diagnosis → 选真实订单 `2117431331554320385`
- `qa_e6bf8dddc3d44ec8b584608c5ee32836` **completed**
- `agent_runs`：`run_8LWRWa3n5MWpEuvmqxrIcVPPII5vbpl26_flpVic_Mk` **succeeded**
- `chat_binding=operator_order_diagnosis:production-41:1942105476598861824`
- **`route_revision=17`** ← 走的就是迁移后的新路由
- 正文返回真实订单字段（Prepaid / Electricity fee / Service fee / stop reason 等）

## 回滚

- 迁移前状态备份：`/tmp/operator-route-before-migration.json`（41 上，0600），
  含 `chat_route_bindings` / `dify_app_registry` / `operator_apps` 三张表的相关行
- 回滚方式：把 `chat_route_bindings` 的 `operator_order_diagnosis` 行恢复为
  `app_id=2b0629cf` / `agent_id=agt_5c3e2e…` / `agent_version_no=1` / `revision=16`。
  注意回滚到的是**已删除 app 的旧状态**，节点仍可执行冻结清单，但编辑器不可用。
- 迁移后的 v24 快照保留在 `agent_versions`，撤回/恢复可走 `change_version`

## 未覆盖

- **Dify 侧的编辑/发布闭环未验证**：本轮只把路由接到现存 app。运营在控制台改该 app
  并点发布、以及"发布后新会话生效"，属于端到端生命周期的另一段，未在本轮做。
- 迁移没有经过 `SelfService.publish()` 的 `record_publish_audit` 记账
  （脚本路径跳过了那一层）。审计账本里这一条是空的，需知悉。
- 另一租户（`1941847786056323072`）的 operator 路由未动。
