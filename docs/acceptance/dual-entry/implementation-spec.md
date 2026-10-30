# 客户双端 AI 服务开通与自主管理：实施规格

日期：2026-10-30。状态：实施规格（不是已上线声明）。
来源规划：[docs/agents/dual-entry-onboarding-plan.md](../../agents/dual-entry-onboarding-plan.md)（只读追溯，不改写）。
任务书：[docs/agents/dual-entry-claude-task.md](../../agents/dual-entry-claude-task.md)。
验收合同：[acceptance.feature](acceptance.feature)；执行映射：[qa-plan.md](qa-plan.md)；
子任务与依赖：[tasks.md](tasks.md)。

本文件只写**这次要实现什么、边界在哪、用什么判据验收**。产品结论以规划为准；
本文件与规划冲突时以规划为准并在此记录差异。

## 1. 术语

| 术语 | 含义 |
| --- | --- |
| 端（entry） | `consumer`（客户端客服）或 `operator`（管家）。取值与 `faq.PLATFORMS`、`dify_app_registry.BUSINESS_ENTRIES` 同一份 |
| 场景（scenario） | 端在执行层的具体路线：`consumer_service`（客户端 Chatflow）、`operator_order_diagnosis`（管家 node-SOP） |
| 开通范围（scope） | `both`（默认，双端）/ `consumer`（只开客户端）/ `operator`（只开管家） |
| 受管应用（managed app） | 平台登记在册的、属于某客户某端某场景的 Dify 应用。目录与正式指针分离 |
| 正式指针 | `chat_route_bindings` 里 `active` 的那一行：`(环境, 租户, 端, 场景)` 此刻由哪一版服务 |
| 草稿 | 受管应用在 Dify 里的当前内容（Dify 无草稿区，保存即当前内容）；未推进正式指针 |
| 渠道 | 该端已接入的使用入口（原 App/H5/小程序）。未接入即"AI 已准备，渠道待接入" |

## 2. 范围

**做**：双端目录与来源选择、统一进度与逐端资源状态、客户端应用自动准备（应用/依赖/凭据/
业务连接/知识引用）、双端草稿预览与逐端发布/撤回/恢复、受管客户端发布目标替代逐客户文件登记、
现役资源纳管、配套部署与真实双端验收。

**不做**（规划 §8 明列的边界）：新域名/小程序/安装包接入、公网暴露、账号提权、额外主机、
模板批量升级、跨环境搬迁、模板市场、多实例统一凭据服务、任何业务写动作（重算/补发/退款/改单）。

## 3. 数据模型

### 3.1 `operator_apps` → 受管应用目录（加两列，不改名）

表名保持不变（既有部署的库文件里已经有这张表，改名要迁移两次而收益为零）。

```sql
ALTER TABLE operator_apps ADD COLUMN entry    TEXT NOT NULL DEFAULT 'operator';
ALTER TABLE operator_apps ADD COLUMN scenario TEXT NOT NULL DEFAULT 'operator_order_diagnosis';
```

- 存量行按默认值补齐：它们全部是管家，与现状一致，不需要数据回填。
- 主键仍是 `app_id`：一个 Dify 应用只有一个归属端，同一应用不能既当客服又当管家。
- `profile_id` 对 `consumer` 为空串：客户端没有 node profile，它的运行目标由
  `consumer_targets` 决定（见 3.2）。

### 3.2 受管客户端运行目标（替代逐客户文件登记）

`operator_apps` 里 `entry='consumer'` 的行是**运行目标的第一权威**；`targets_json` 列保存该端的
可发布能力（`knowledge_base_ids` / `live_order_status` / `live_billing_record` / `photo_enabled`）。

```sql
ALTER TABLE operator_apps ADD COLUMN targets_json TEXT NOT NULL DEFAULT '{}';
```

读取顺序（`consumer_publication.target_for`）：

1. 受管行命中 `(environment, tenant, app_id)` → 用它，`source="managed"`。
2. 否则读现役 `AIOPS_CONSUMER_PUBLICATION_TARGETS_FILE`（0600 JSON），`source="file"`。
3. 两边对同一个 `(environment, tenant)` 指向**不同** app 时**显式拒绝**（
   `consumer publication target conflicts with managed registration`），不按"谁优先"裁决。

这条规则的作用：现役客户文件继续生效（不重建、不改登记），新客户只走受管行；
同一个客户不允许出现两条互相矛盾的正式目标。

### 3.3 开通任务：`scope` 与逐端资源

`operator_setup_jobs.request_json` 增加：

```json
{"scope": "both",
 "sources": {"consumer": {"app_id": "...", "version_no": 7},
             "operator": {"app_id": "...", "version_no": 3}},
 "entries": ["consumer", "operator"]}
```

`resources_json` 增加逐端状态（`entries`），每端：

```json
{"app_id": "...", "version_no": 7, "published": true, "draft_only": false,
 "key_ref": "<0600 路径>", "knowledge": "ready", "channel": "connected|pending",
 "state": "ready|draft|failed", "error_code": ""}
```

顶层 `app_id` / `version_no` / `published` 继续镜像**管家**端（`operator`），
保持既有页面与既有契约兼容；没有管家端时镜像第一个已选端。读取方一律优先读 `entries`。

步骤名沿用既有词表并加端前缀：`workspace` → `knowledge` → `dependencies` →
`<entry>:application` → `<entry>:key` → `<entry>:register` → `<entry>:publish` → `complete`。
重试从 `step` 续跑；已完成端不重复创建、不重复发布。

## 4. 行为契约

### 4.1 目录（`GET /v1/dify/management/catalogue`）

在既有响应上**新增** `entries` 数组，每项：

```json
{"entry": "consumer", "label": "客户端客服", "scenario": "consumer_service",
 "sources": [{"app_id": "...", "tenant_id": "...", "tenant_name": "...",
              "app_name": "...", "name": "<客户名> / <应用名>", "version_no": 7}]}
```

- `sources` 只列**该端**、`active`、且已登记为受管应用的正式路由；客户端不再被过滤掉。
- 顶层 `sources` 保持原样（管家端），既有前端不改也能跑。
- 平台账号可选客户仍来自公司 `/upms/tenant/list/my`；非平台账号只能看见自己租户。
- `operations[]` 每项增加 `scope` 与 `entries` 进度，页面据此显示"部分完成"。

### 4.2 开通（`POST /v1/dify/management/operations`）

- `scope` 缺省 `both`。`consumer` 端必选客户端来源，`operator` 端必选管家来源；
  缺一端即 `SETUP_SOURCE_REQUIRED`，不把一端应用改个标签当另一端。
- 来源必须**已发布**（`active`）且**属于该端场景**：`SETUP_SOURCE_NOT_PUBLISHED`。
  版本号与当前正式版不一致 → `SETUP_SOURCE_CHANGED`（409）。
- 同一 `request_key` 冻结的 `scope`/`sources` 不同 → `SETUP_REQUEST_CHANGED`（409）。
- 双端默认**不是一个不可分割事务**：一端失败，另一端继续并各自记录状态，整体 `state="partial"`。
  重试复用已完成端。

### 4.3 客户端准备（`consumer` 端步骤）

1. **应用**：从所选来源应用导出 DSL（`advanced-chat` 必须是 Chatflow），把知识选择节点的
   `knowledge_base_ids` **重新绑定**到目标客户的对客库；源客户库 UUID 一律不得带过来
   （带过来即 `SETUP_KNOWLEDGE_CROSS_TENANT`）。导入用确定性 `request_id`，
   应用 id 由 `uuid5(namespace, workspace_id + ":" + job_id + ":consumer")` 推出，
   响应丢失后按同一 id 回读，不重复建应用。
2. **依赖**：模型/插件/provider 复用 `self_service_prepare.dependencies`（公共准备，只做一次）。
3. **凭据**：`GET /console/api/apps/<id>/api-keys` 先取快照 → 需要时 `POST` 建一枚 →
   再取快照核对"新增恰好一枚"→ 以 `0600` 原子写入 `<key_dir>/<app_id>.key`。
   创建超时先对账：新增恰好一枚则复用（1.17.1 的 app key 列表返回完整 token），
   0 枚或 ≥2 枚即 `SETUP_KEY_AMBIGUOUS` 并**不**切正式指针；达到 10 枚上限即
   `SETUP_KEY_LIMIT`。既有文件里的 key 仍在该 app 的 key 列表里才复用，否则重取。
4. **业务连接**：把该客户的 `charging_endpoint`/CPMS 目标租户登记为受管运行数据
   （客户端取数仍走 `ConsumerToolGrants`，不新增任意租户兜底）。
5. **知识引用**：`targets_json.knowledge_base_ids` 记录该应用选定的对客库；发布时再核一次
   "属于该租户 + 允许对客 + 在发布选择内 + 当前可用"。

### 4.4 发布 / 撤回 / 恢复（逐端）

- `POST /v1/dify/management/publish`：既有管家路径不变；`entry='consumer'` 的受管应用走
  `sync_workflow_from_dify`（冻结 `consumer_service` 设置、发布 workflow、推进正式指针），
  发布后回读 key 可用、知识库可用、渠道状态，写入逐端状态。
- 已存在该端正式应用时，开通**只建草稿**（`draft_only=true`），不替换现役正式版；
  只有客户主动点发布才推进指针。
- 撤回 = `chat_route_bindings` 改 `retracted`（不删行、不回落直读）；恢复 = 推进到指定历史版本。
- 发布/撤回都按 `revision` 做 CAS，冲突即 `SETUP_ROUTE_CHANGED`，不覆盖。

### 4.5 草稿预览

- 客户端草稿预览不要求先有正式版：试用绑定**当前受管草稿**（`app_id` + 当前 workflow），
  不推进正式指针，不依赖 `target_for` 文件登记。
- 预览失败给可操作原因，不返回空白答复。

### 4.6 租户自主管理

- 客户在自己工作区改提示词/选库/预览/发布/撤回/恢复，走既有 Dify 页面 + 同源桥
  （`/__aiops/manage`），不新增后台。
- 改一端或一个租户不改变另一端/其他租户：受管行按 `(tenant, entry, scenario)` 分键，
  发布 CAS 只作用于本键。

### 4.7 渠道与就绪口径

- 每个受管端记录 `channel`：`connected`（该端已接入的原 App/H5 已能服务）/
  `pending`（AI 已准备、渠道待接入）。判据来自环境清单 `ops/environments/<env>.toml`
  的已接入渠道声明，不由"应用已创建"推导。
- 页面口径分开显示：资源准备 / 该端已发布 / 渠道已接入 / 真实用户已验证。
  没有真实账号验证时标"待业务验证"，不写"开通完成"。

## 5. 安全与边界

- 凭据：只写服务身份专属 0600 目录；不进仓库、不进日志、不进任务 JSON（只存路径）。
- 授权：开通/发布/撤回都重新校验操作者身份与目标租户（`auth.require_target`），
  重试也重新校验；不因"上次通过"而跳过。
- 客户端知识隔离：发布与实际检索两处都校验；隐藏下拉项不算隔离。
- 本规格不新增任何业务写动作，不新增公网入口，不新增主机。

## 6. 与规划的差异

| 规划表述 | 本次实现 | 理由 |
| --- | --- | --- |
| "受管应用目录与正式指针分离" | 目录落在 `operator_apps`（加 `entry`/`scenario`/`targets_json`） | 复用既有表与既有部署；改名要迁移两次，收益为零 |
| "客户端发布目标引用受管运行配置" | 受管行为第一权威，现役文件保留并显式冲突拒绝 | 现役客户零迁移；两条权威同时存在时必须报错而不是隐式优先 |
| "管家默认有知识空间不代表会检索" | 本次不改管家 KB 能力 | 管家发布仍拒绝带 KB 的草稿；模板能力另行交付 |
