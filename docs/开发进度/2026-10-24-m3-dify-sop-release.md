# 2026-10-24 · M3-01：SOP 编辑→试用→发布→撤回 真正控制 App 的 Dify 执行

## 目标

运营在既有编辑入口保存草稿**不影响** App；发布才切版本；撤回后不再交付该版，
且**不回落直读**。复用既有版本/审计/权限，不另造管理系统。

## 任务 0 实测基线（与任务书不一致，按实测记）

| 项 | 任务书 | 实测 |
|---|---|---|
| 41 health | `cf9a55f`（#1055） | **`0.2.0+9360fa4f2a75`**（#1054，在 #1055 之后合并） |
| 三键 | 未设置 | 未设置 ✓ |
| `gateway.env` | 摘要未变 | `0fb2500647d8e920` |

分支从 `9360fa4`（当时最新 `origin/main`）建，不是 `cf9a55f`。

## 现状缺口（勘察结论）

代码里没有以 SOP 命名的模块，现状是五块互不相连的机制：

* `agent_lifecycle`：`agents.published_version` + 不可变 `agent_versions`，没有撤回；
* `dify_console_publish`：桥把运营的自己控制台会话验一遍，再以机器身份调 `/v1/dify/publish`；
* `sync_binding_from_dify`：把 DSL 冻结成 AgentVersion，**不碰聊天绑定**；
* `dify_trial`：跑的是 `run_agent_diagnosis`，**不是** Dify Workflow；
* `chat_execution`：绑定是运维手填的 0600 JSON，**生产代码里零个写入者**。

缺口就是一句话：**发布与路由之间没有代码路径**。

## 本片做法

| 层 | 改动 |
|---|---|
| 新模块 `chat_route_bindings` | `chat_route_bindings` 表：正式指针。`agent_id` + `agent_version_no` 指向不可变版本；`app_id` / `workflow_id` / 版本号**三列分开**；`revision` 乐观锁；状态只有 `active` / `retracted` |
| 发布 | `_publish_bound_app` 成功后 `_advance_route_binding` 推进指针；拿不到 workflow 身份 ⇒ **不推进**并报 409，不拿 app_id 顶替 |
| 工作流身份 | `DifyDslClient.published_workflow_id()`：`GET /console/api/apps/{id}` → `workflow.id`。**取不到不阻断版本冻结**（CLI 与既有部署不依赖指针），但把原因写进 `note` |
| 撤回 | `POST /v1/dify/retract`，与发布**同一道门**；改状态不删行；收敛在飞运行；恢复只能靠显式发布 |
| 读路径 | `_bindings` = 文件登记 + 管理表（仅 `active` 行）；同一 key 两边都有 ⇒ **启动失败** |
| 回读 | 冻结 `agent_version_no`；当前绑定不再是那一版 ⇒ 403；已撤回 ⇒ **410 `CHAT_ROUTE_RETRACTED`**（与"没配过"分开） |
| 凭据 | 管理行只存**路径** `<key_dir>/<app_id>.key`；发布全程不读密钥 |

## 验证

* 新增 `tests/test_chat_route_bindings.py`（20 条）：指针推进、撤回不删行、幂等、
  不恢复旧版、两租户/两环境互不影响、CAS 冲突、管理行与文件行冲突、三组反向验证。
* 新增撤回路由测试 6 条（`tests/test_dify_publish_api.py`）：同一把门、未配置 404、
  越权撤回 409、请求体不能选版本。
* 全量 **3814 passed / 9 skipped**。

## 这一轮踩到的三个真问题

1. **把新外部调用做成了硬性前置**：`sync_binding_from_dify` 一度强制读 app 详情，
   把 CLI 与既有部署的假上游全部打红。改成"读不到就不推进指针、如实标注"，不阻断版本冻结。
2. **`SCENARIO_OWN_ORDER_STATUS` 少了模块前缀**（`F821`）——新路由会在运行期 `NameError`，
   只有 lint 抓到。
3. **用 `git checkout` 还原变异，抹掉了整份未提交的实现**（`chat_execution.py`）。
   这与既有教训一致：还原变异一律用备份文件或内存原文写回，不用 `git checkout`。
4. **撤回一度回落直读**（最严重的一条）：撤回把管理行改成 `retracted` 之后，
   `select_binding` 查不到活动绑定就返回 `None` —— 而 `None` 的既有含义是
   "从来没配过，走原直读"。于是"撤回"变成了"悄悄换一条路继续交付"，用户照样拿到
   completed。修法是把两种情形分开：**没配过 ⇒ None（兼容）；撤回过 ⇒ 410 拒绝**。
5. **验收脚手架的一串假绿**：`with sqlite3.connect()` 不提交、手写 INSERT 与真实表结构
   列数不符、`roles={"admin"}` 而非 `ROLE_*`、`AgentManager(GatewayStore)` 而非
   `AgentStore`、空绑定表当成"没有绑定"、app key 当 console 凭据、
   循环变量 `key` 遮蔽了 app service key（写进凭据文件的是 `"b"`）。
   每一个都让前置"看起来做了其实没做"，而症状出现在很远的地方。
   其中两处同时暴露了**判据太松**：`bool(key)` 对 `"b"` 也为真；
   只看 `completed` 分不清"执行了"与"掉回直读"。

## 真实整链结果（43/43 PASS）

`tools/m2_close/m3node/verify_dify_chat_entry.py`，2026-10-24：

| 步骤 | 实测 |
|---|---|
| 前置（注册表 / workspace / agent） | 三者就绪 |
| 保存 V2 草稿 | 真的有 dify run，实际执行 **V1** |
| **真 Dify 试用 V2** | 临时应用里真执行（模型收到含这张单的播报输入、CPMS 被按这张单读一次），正式指针、版本、任务行**都没动** |
| 发布 | 200，`action: synced`，`version_no: 2` |
| 发布后 | 真的有 dify run，实际执行 **V2** |
| 撤回 | 200，`state: retracted`，`revision: 3` |
| 撤回后新请求 | **没有执行**（无 completed、无直读正文） |
| 撤回后历史正文 | **410 `CHAT_ROUTE_RETRACTED`** |
| retracted 行 | 仍在（不删行） |
| 版本列表 | 能看见 `[1, 2]` |
| 显式恢复**历史 V1** | 实际执行的就是快照 V1 的 `workflow_id`，且 ≠ V2 |
| 双环境交错 | 另一环境自始至终 `revision=1 / version=9 / active` |
| **双租户交错** | 第二租户自己的 app 经同一发布入口推进；撤回主租户后它仍 `active`、revision 不变、App 照常执行自己的应用 |

每条判据都额外要求"这一轮真的走了 Dify"（执行记录里有 `workflow_run_id`）——
直读同样返回 completed，只看 completed 会让"掉回直读"与"确实执行了"无法区分。

## 仍未完成

真实公司 UPMS 权限、真实供应商与生产库、真机验收都没做；生产未启用。
试用与双租户都跑在**同一个隔离 Dify 实例**上（合成模型/CPMS/目录），
不构成真实故障结论。
