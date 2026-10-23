# 建库同步挂到「添加知识库」弹窗：验收记录（#1056 / ADR-0122）

**范围**：`src/aiops_diagnostics/{dify_console_publish,gateway_api,gateway_runtime,gateway_config}.py`、
`knowledge_dataset_sync_guard.py`（新）、`ops/dify-console-bridge/dify-console-publish.conf`、
`deploy/dify-36/web-patches/1.17.1/02-web-sync-on-knowledge-picker.patch`（新）、`tests/`
**性质**：新增写能力的网关与前端行为。**未做真机验收、未部署**，见文末。

## 一、要解决的问题

运营对"打通"的期待是：**在 RAGFlow 建一个库，它就该出现在 Dify 里。**

真机上出现过两次"我建了库，Dify 没有"，每次都需要运维跑
`aiops admin sync-knowledge-datasets`。运营看不到、也跑不了那条命令。

## 二、两次走错的路（都记在这里，因为它们定义了现在这个形状）

### 第一次：触发点放在我们自己的清单路由

`GET /v1/dify/knowledge` 看起来是"选择器会调它"。**实际只有「创建外部知识库」表单
会调**（`ExternalApiSelection.tsx` → `ExternalKnowledgeIdPicker`），而运营要的是编排页的
**「添加知识库」** —— 它走 Dify 自己的 dataset 列表，根本不经过我们的清单路由。

同一次 review 还抓到一个 P1：接线时把 `knowledge_base_id` 改成了 `dataset_id`，而
`libraries_for` 只认前者 ⇒ **每一行被静默跳过**，同步报成功、零写入。

### 第二次：删旧实现时删多了

换到前端补丁后，我把挂在清单路由上的旧代码删了 —— **连带把并发闸也删了**。
review 指出：两个运营同时打开弹窗就是两个并发 pass，各自读一遍列表、各自决定建
同一个，而 Dify 对 `(tenant, name)` 没有唯一约束，输的一方留下重复。

同时指出：桥用的是 `AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID`，那是**迁移期的静态回退
值**，不是"当前活跃 workspace"。多 workspace 部署里会同步错租户，而运营看到的
是自己那一页 —— 错误不报，只表现为"新库没出现"。

## 三、现在的形状

```
编排页「添加知识库」弹窗打开
   → POST /__aiops/knowledge-sync?workspace=<当前页的 workspace>
   → 桥验 Dify 会话（租户只能来自这里）
   → 网关 POST /v1/internal/knowledge/sync（内部令牌）
   → 按 workspace 反查租户 → 并发闸 → 收敛 RAGFlow 库为 Dify dataset
   → 有变化就刷新列表
```

## 四、判据与实际结果

| # | 判据 | 结果 |
|---|---|---|
| 1 | `tenant_id`/`tenant`/`environment` 出现在请求里 → **400**（#979 那个洞的形状） | ✅ |
| 2 | 空 workspace → 400，不回落 | ✅ |
| 3 | 未配 API 模板 → 404（不猜模板） | ✅ |
| 4 | kb-service 不可达 → `created: 0`，不是 5xx | ✅ |
| 5 | 未登记的 workspace → 404（只查不建） | ✅ |
| 6 | 租户来自 workspace 反查（kb-service 按那个租户列库） | ✅ |
| 7 | 并发时只有一个 pass 真的跑 | ✅ |
| 8 | 成功下限 15s / 失败下限 5s（抖动后能重试） | ✅ |
| 9 | 闸绝不抛（调用方在弹窗路径上） | ✅ |

**变异复验**：

| 变异 | 转红 |
|---|---|
| 去掉并发锁 | 3 条 |
| 失败下限改成与成功相同 | 1 条 |

## 五、前端补丁的验证

```
patch -p1 --fuzz=0 --dry-run   → ok      与 build-web-image.sh 实际参数一致
git apply --check              → ok
npx tsc --noEmit               → 改动的两个文件零类型错误
```

`tsc` 跑的是 **Dify 1.17.1 的真实源码树**（`git clone --branch 1.17.1`），不是设想。

## 六、未完成的部分（明说）

- **未做真机验收。** 本片只到单元与路由层。补丁还没烧进镜像，容器还没换。
- **未部署。** 需要：重建 web 镜像 → 换容器（期间控制台短暂不可用）→ 41 配
  `AIOPS_GATEWAY_DIFY_EXTERNAL_KNOWLEDGE_API_ID`。按用户决定分两步，第二步单独确认窗口。
- **前端渲染未在真实浏览器里验过。** `tsc` 通过不等于"弹窗打开时真的会发那个请求"。
- **运营侧流程未验**：建库 → 打开弹窗 → 新库出现在列表里，这条完整动作没跑过。

**结论：未完成业务验收。**
