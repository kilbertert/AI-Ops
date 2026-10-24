# 127. 发布才切路由，撤回是改状态而不是删绑定

日期：2026-10-24
状态：已采纳
影响版本：`src/aiops_diagnostics/chat_route_bindings.py`（新增）、
`chat_execution.py`（`_bindings` / `select_binding` / `_retracted`）、
`gateway_api.py`（`_advance_route_binding` / `POST /v1/dify/retract`）、
`gateway_runtime.py`（`settle_retracted_runs`）、`dify_dsl_pull.py`
（`sync_workflow_from_dify`）、`dify_run_store.py`（`agent_version_no` 列）

## 背景

ADR-0124 把「App 聊天交给 Dify 跑」接好了，但那条 ADR 与管理面之间还缺一环，
ADR-0104 的「仍未做」里点名了它：**SOP 发布绑定**。

接线之前的实际状况是：

* 运营在 Dify 控制台点「发布到 AI-Ops」→ `sync_binding_from_dify` 把 DSL 冻结成
  一个 `agent_versions` 行；
* App 跑的却是**运维手填**的 `workflow_id`；
* 两者之间没有任何代码路径。

也就是说：**运营点了发布，App 跑的还是上一版**，而界面上没有任何东西显示这件事。

## 决定

### 1. 正式指针是一张表里的一行，不是一个标量列

新表 `chat_route_bindings`，主键 `(environment, tenant_id, entry, scenario)`，
行上冻结 `engine`、`agent_id`、`agent_version_no`、`app_id`、`workflow_id`、
凭据**路径**、`state`、`revision`。

不复用 `agents.published_version`：那是**内容**的权威（可以被试用、可以被多个入口共用），
而这里是**路由**的权威（哪个入口此刻走哪一版）。一个租户可能同时有运营入口与 consumer
入口指向不同版本，把它们塞进一个标量列里，第一次出现第二个入口就要改表。

`app_id` / `workflow_id` / 版本号是**三列分开**的。把同一个值填进多列，等于三列都在撒谎。

### 2. 撤回是改状态，**永不删行**

撤回之后这一行仍在，`state = 'retracted'`。

**删行会让请求掉回直读。** `select_binding` 的 `None` 有一个既有含义——「没配过，
走原直读」——而撤回的语义是「停止交付」。两者混在一起，撤回就变成"悄悄换一条路
继续交付"：用户照样拿到一个 `completed` 的答案，只是它来自另一条路径。这是本片
实测撞上的失败形态，也是最难发现的一种。

所以 `select_binding` 在找不到活动绑定时，要**再看一眼管理表**：
没配过 ⇒ `None`（兼容）；撤回过 ⇒ `410 CHAT_ROUTE_RETRACTED`（拒绝）。

### 3. 恢复是另一次显式发布，不是撤回的副作用

撤回只把指针标成 `retracted`，**不**自动回退到上一版。恢复旧版走一次正常的
`POST /v1/dify/publish`（它会把状态改回 `active`）。隐式回落会让"现在跑的是哪一版"
变成一个需要推理的问题，而不是一个可查的事实。

### 4. 发布与撤回共用同一道门

`POST /v1/dify/retract` 用 `AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`，与发布同一把。
能发布的人才能撤回，不新造第二套权限。

### 5. 文件登记与管理表不能同时声明同一个 key

违反 ⇒ **启动失败**。不裁决优先级：那会让"这次到底谁在跑"从一个事实变成一个猜测。
迁移的形状是「不再配文件路径，改由管理表接管」——注意**写一张空表是配置错误**，
不是"没有绑定"。

### 6. 发布时凭据文件必须已就位

管理行只存凭据**路径** `<key_dir>/<app_id>.key`，发布全程不读密钥。但路径必须可读：
一条指向不存在文件的路径会发布成功、审计也记了，而之后每次请求都 `DIFY_UNAVAILABLE`
——那个症状离发布这一步非常远。

### 7. workflow 应用与 chat 应用是两条映射

`sync_binding_from_dify` 只吃 chat/completion（它们有 `model_config`）。workflow 应用的
内容在 `workflow.graph` 里，硬套会得到 `DSL has no model_config object` —— 那看起来
像"DSL 坏了"，其实是"这是另一种应用"。新增 `sync_workflow_from_dify` 冻结
`{kind, app_id, workflow_id, workflow}` 快照，住同一张 `agent_versions` 表，但**不假装**
它是 AgentConfig。

## 备选方案（均被否）

* **复用 `agents.published_version` 当路由指针** —— 被否：它既是内容的权威又要当路由的
  权威，第二个入口出现时无法表达"两个入口走不同版本"。
* **撤回 = 删掉绑定行** —— 被否：删行后请求掉回直读，用户照样拿到答案，只是来自另一条
  路径。这正是本片实测撞上的失败形态。
* **撤回后自动回退到上一版** —— 被否："现在跑的是哪一版"会变成一个需要推理的问题；
  恢复应当是一次可审计的显式发布。
* **文件登记与管理表按"表优先"裁决冲突** —— 被否：隐式优先级会让"这次到底谁在跑"
  变成猜测；启动失败是唯一诚实的处理。
* **把 workflow 应用硬塞进 chat 的 DSL 映射** —— 被否：它没有 `model_config`，
  塞进去只会得到一个看起来像"DSL 坏了"的报错。

## 代价与已知限制

* Dify 的 inputs 与运行记录会保存本次 input，取数凭据对**能打开该应用记录的人**可见
  （ADR-0124 已写明，本片未改变）。
* 已发出的数据不承诺追回 —— 撤回停止的是"后续交付"，不是"已交付内容"。
* 本片只覆盖 Dify 路线；同一发布语义控制真实 Java 节点是 M3-02，**未完成**。
* 真实公司 UPMS 权限、真实供应商与生产库、真机验收均未做；生产未启用。
