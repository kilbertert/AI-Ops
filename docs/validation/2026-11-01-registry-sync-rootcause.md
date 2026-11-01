# 登记不一致的根因与修复（2026-11-01）

## 结论：不是漏更新，是两条路线各管各的

| | `dify_app_registry` | `chat_route_bindings` |
|---|---|---|
| 写入者 | 环境清单 `aiops-admin reconcile` | 开通作业 + `_advance_route_binding` |
| 语义 | "该租户的 consumer 答案来自哪个 Dify 应用" | "当前生效的运行时指针" |
| 10-31 12:15 后 | 仍是 `394b5399`（清单 10-30 09:42 的旧值） | 已切到 `07d15c42` |

**全仓 `registry.put()` 只有一个调用点**：`agent_manifest.py:383`，由环境清单驱动。
开通流程（`operator_setup_jobs`）创建新应用时**从不写 registry**。
于是 12:15 换了应用，registry 留在旧值，两条路从此不一致。

## 现场清单

```
/opt/aiops-41/ops/environments/env-41.toml:130-134
[[dify_apps]]
tenant_id = "1942105476598861824"
business_entry = "consumer"
app_id = "394b5399-7ad9-428e-b2b0-cc7f05720a81"   ← 10-30 的旧值
agent_name = "wf01-consumer-service"
```

## 修复（已执行）

1. 备份：`/tmp/env-41.toml.bak-20261101T070554`
2. 清单 `app_id` → `07d15c42-c2a1-5432-8081-30e5f5dd95b0`
3. **只收敛 consumer 那一行**，没有走整份 `reconcile`，原因两条（都是既有保护，不是绕过）：
   - 整份 reconcile 先跑 agent 收敛，被模型白名单挡住（`deepseek-v4-flash` 不在 `deepseek-v4-1-flash-261017` 白名单）；
   - `reconcile_dify_registry` 会拒绝覆盖**页面管理**的 `(tenant, operator)` 绑定 —— 清单里那条 operator 行本就该由页面管理。
4. 结果：`consumer` → `07d15c42`，`operator` 未动。

## 修复后的验证

```
POST /v1/dify/publish {"app_id":"07d15c42…"}
→ 200 {"action":"synced","version_no":10}
```

绑定推进：`revision 9 → 10`、`agent_version_no 10`。**产品发布路径恢复可用。**

## 关键机制（本次才看清）

`callback_version` 在**发布时**从 Dify 当时的 workflow 计算并冻结进绑定
（`gateway_api.py:1327 publication_settings`）。所以正确顺序是：

**先在 Dify 改成 slots 版 → 再用产品端点发布** → 绑定才会冻结成 5。

我第一轮反了：先用 Dify 原生 publish 发 slots，再用产品端点 —— 而当时草稿已被回滚成 A，
产品端点冻结的是 A，`callback_version` 仍是 4。**这不是缺陷，是我顺序错。**

## 当前状态

正式会话实测 `fact_contract: legacy`（因为发布时 Dify 内容是 A）。
线上是 A 版，与回滚前一致。**业务验收未完成。**
