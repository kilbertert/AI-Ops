# Dify 租户运维首批工具

> 2026-10-28适用范围：早期租户服务身份两工具；非现役管家个人节点授权。当前入口见[更新说明](../operator-cpms/README.md)。
> 下文保留原时点事实；其中“当前”“未完成”不得覆盖后继已验收结果。

规格 #934 / 子任务 #935。首批只读订单列表与当前状态，不做故障归因或修复。
新路由 `POST /v1/dify/ops/{tenant_id}/order_list`、`charging_info` 不接受个人登录会话，
只认独立服务 key；它可读登记租户数据，不能直接供 consumer/self 使用。

2026-10-22 当前状态：首次旧凭据失败保留，#960 修复后的另次授权预览模型段已通过；
随后 operator 两工具常驻启用，回读发布 v21（原七工具加两新工具），consumer/M3 配置仍关闭。
真实模型段通过不等于公司 App 或故障准确率验收；具体证据与离线重验见[模型验收](model-acceptance.md)。

## 私有绑定与地址

网关配置 `AIOPS_GATEWAY_OPS_BINDINGS_FILE` 指向当前服务身份所有、0600 的 JSON 数组文件。
独立 key 至少 32 字符，只将 SHA-256 存入绑定；不要复用目录、发布或控制台密钥。

```json
[
  {
    "key_sha256": "<独立随机密钥的64位小写SHA-256>",
    "environment": "<部署环境>",
    "tenant_id": "<租户>",
    "workspace_id": "<已登记workspace>",
    "app_id": "<该租户operator入口的Dify app>",
    "agent_id": "<同入口的本地agent>",
    "active": true
  }
]
```

配置只授予已登记的 operator 绑定，不按同名智能体猜测。禁用或删除该凭据行下一次调用即拒绝。
禁用行保留时，发布仍理解新工具的空名单语义。文件丢失/不可读不回落。

`AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL` 为逗号分隔 `tenant:https://business/base`，
同地址的多个租户分别登记。无对应行的新工具返回 `TENANT_ENDPOINT_UNRESOLVED`。
此键只供运维工具使用，不启用 consumer 状态入口，也不要求 M3 的逻辑执行环境。
专用键为空时兼容首片的 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 租户映射；
专用键非空但缺目标租户时拒绝，不回退旧地址。旧键的历史单一 URL 仍不为新工具兜底。
旧键同时控制 consumer 状态入口，首次只启用运维工具时使用专用键。修改配置经正常部署生效。
数据库来自同一网关的既有 MySQL 配置，地址表不能让数据库自动跨客户切换。

## 登记、发布与调用

先通过现有开户/清单建立 workspace、operator app/agent 绑定和初始发布版本，
再准备私有绑定、独立 key 文件及本环境地址配置。登记命令：

```bash
aiops --config <网关配置文件> admin ensure-ops-provider \
  --tenant <租户> --token-file <私有key文件> \
  --gateway-url <本环境Gateway的HTTPS地址> --db <已存在gateway.db>
```

命令复用指定 workspace 的 console client，走 Dify `/tool-provider/api/add`，不切共享 current，
不覆盖现有 schema。相同 schema 重跑为 `unchanged`；Dify 回读密钥是掩码，
因此输出 `credentials_verified=false`，**不能把 unchanged 当成凭据已验证**。凭据轮换需单独验证，
命令不会静默覆盖旧 provider。网络失败可能已创建，重跑先查现状。

新建成功后用同一payload再走官方`/tool-provider/api/update`：Dify 1.17.1删除/同名重建
不会清除按workspace/provider名字保存24小时的凭据缓存，update会清除；
已有provider的`unchanged`分支仍然零写，不把重跑当轮换。缓存刷新失败明确报“已创建但未确认”，
不能记录`created`成功；后续`unchanged`也不证明恢复成功，须按下述方法处理并真实验证。

### 同名provider重建后凭据缓存的恢复方法

实际症状：本轮key与绑定摘要一致，模型工具请求仍`403 OPS_BINDING_REJECTED`，provider回读掩码仍对应旧key。
只在确认是本轮新建、已授权的provider后，用服务身份读取key，构造完整`api/update` payload：
`original_provider`与`provider`均`aiops-ops`，schema/icon/privacy_policy/custom_disclaimer取该provider回读值，
credentials为独立key及Bearer四字段；经目标workspace的console client写回，再回读掩码与本轮key掩码比较。
不要直接删Redis、重启Dify或复用旧key；接口原样重写仅用于新建provider的缓存恢复，不覆盖别人的配置。
该管理路径不调用模型或业务；掩码匹配只证明回读变更，下一轮真实工具/模型调用仍需验收。

行为依据为Dify 1.17.1
[`ProviderConfigEncrypter.decrypt`](https://github.com/langgenius/dify/blob/1.17.1/api/core/helper/provider_encryption.py)
优先读缓存、[`SingletonProviderCredentialsCache`](https://github.com/langgenius/dify/blob/1.17.1/api/core/helper/provider_cache.py)
以名字索引并保存86400秒，官方`api/update`提交后调用`cache.delete()`。

在 Dify 编辑对应 app，勾选 `aiops-ops` 工具并通过既有 AI-Ops 发布入口发布。
合法目录与历史默认权限分开；新工具不因升级默认获权。新 provider 全关闭/删掉后发布，
新权限清空；未声明旧 provider 时保留旧已发布七工具权限，避免迁移顺带关掉旧业务。
需要撤旧工具时明确保留其声明并关闭，或者使用既有版本管理发布显式名单。

请求体：`charging_info` 为 `{"order_no":"..."}`；`order_list` 为
`{"page":1,"page_size":20}`，页码最大1000、页大小最大50。额外身份/地址字段拒绝。
响应为 `result` 与 `evidence`；后者含调用编号、登记目标、权限版本与结果 SHA-256。
成功及错误均 `Cache-Control: no-store`。证据落 `ops_tool_audit`，不存 key 或完整业务正文。
授权失败在业务取数前拒绝；读取过程中发布/绑定变更则丢弃结果；证据写入失败不返回成功正文。

Dify 预览跑 Dify 循环；旧“在 AI-Ops 试运行”仍验证旧诊断执行器。
新工具草稿不会被旧试运行假装执行，明确提示使用 Dify 预览。App 与 Dify 个人授权、完整版本发布
及撤回属于后续片；本片不得宣称原 M2/M3/M4 完成。

## 可复跑验收

```bash
uv run pytest -q tests/test_ops_tools.py
uv run python tools/verify_ops_tools_http.py --out /tmp/ops-http-report.json
```

HTTP 脚本在一次性数据库启动真实 Gateway，真实绑定与发布，只有业务读取替身。
它不证明真实 Dify、公司接口或用户业务正确性；真实验收按 [QA](qa-plan.md) OPS-07 单独记录。
证据摘要按结果 JSON 的 UTF-8、sort_keys、无空白分隔计算；传输格式无需逐字相同。

## 部署后的只读复核

正常合并由既有CD部署，查看该合并提交对应的CD回执，再按环境手册复核服务 active、健康版本及源码摘要。
主机访问统一用 `dev-host exec <已登记目标> --allow-service-exec --timeout 30 -- '<只读命令>'`，
主机身份、服务名、部署目录与配置位置取该环境手册；不从监听端口猜目标。

开关要查正在运行的进程：用 `systemctl show -p MainPID --value <服务名>` 取PID，
由具备读取权限的身份在远端Python内读取 `/proc/<PID>/environ`，只输出
`bool(env.get("AIOPS_GATEWAY_OPS_BINDINGS_FILE", "").strip())`；
再用 `dotenv_values` 读该进程 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 所指文件，同样只输出该键是否非空。
**两处都为空才可记录新工具尚未启用**；不要打印整个环境或配置。
M3的 `AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE`、`AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE`、
`AIOPS_GATEWAY_EXEC_CONFIG_REQUIRED` 也只核对是否配置。

源码用 `hashlib.sha256(Path(<部署目录>/src/aiops_diagnostics/<文件>).read_bytes()).hexdigest()`，
与合并提交的同文件字节独立比较；`__init__.py` 含部署版本戳，应按部署脚本生成的制品比较。
仅健康与摘要一致证明部署身份，不证明provider登记、Dify模型循环或真实业务结果。

常驻启用时同时检查 `AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL`，并回读绑定是否 active、
Dify 两工具 enabled 与本地已发布 `allowed_tools`；文件值为空而进程值非空仍是启用。
2026-10-22 的配置/key 位于服务身份私有 `/var/lib/aiops-41/ops-tools/`，权限与摘要在现场核对，
不要打印 key，也不要用 `ops-model-*` 临时窗口的旧备份覆盖它。

### 从既有 Gateway 路径只读核对 Dify

按环境手册使用该服务的 Python，脚本经 stdin 交给
`dev-host exec <目标> --allow-service-exec --timeout 40 -- '<服务Python> -'`；
先读 `/proc/<PID>/environ`，以 `bytes([0])` 切分并断言含 `PATH`，避免把字面的 `\\0` 当成 NUL。
配置用 `dotenv_values(..., interpolate=False)` 读取，进程值优先于文件值。
数据库以 `sqlite3.connect('file:<gateway.db>?mode=ro', uri=True)` 打开：
只查询 `dify_workspaces` 的当前环境/租户、`dify_app_registry` 的 operator 行及对应 agent 的已发布版本。
不要实例化会建表、修改文件权限的 manager/registry 类来完成只读检查。

具体字段：workspace 是 `tenant_id/environment/workspace_id/setup_state`；注册表按
`tenant_id/business_entry='operator'` 读 `app_id/agent_name`，没有 `agent_id/entrance` 列。
agent_id 取受控服务绑定，再对照发布快照的 name 与注册表 agent_name，避免按名称猜身份。

将现场读出的 console 地址、key、workspace 与 app 传给既有 `DifyDslClient(DifyDslSource(...))`；
仅调用 `tool_provider_ids()` 和 `export_dsl()`。只输出 workspace 状态、模型标识、工具启用项、
发布权限和导出 SHA-256，不输出完整 DSL、凭据或业务正文。这条路径复用服务已有的 Dify 连接，
不需要另开主机访问或修改网络。模型已选择不证明额度可用或模型循环成功。

真实验收前的变更范围和恢复条件见[验收变更单](live-acceptance-plan.md)。

### 固定输入真实联调与恢复（2026-10-22 已实跑）

按变更单取得本轮目标/数据/窗口授权后执行，不默认重复上一轮样本。固定输入联调不调用模型，
验的是 Dify 的真实凭据存储、出站、Gateway 发布权限、业务读取与恢复；不是自主模型循环。

1. 基线：服务 Python 读取两份配置、目标 app 完整 `model_config` 与 export DSL、已发布快照，
   0600保存在私有目录；只输出摘要。配置备份取**执行前最新值**，不能恢复几小时前的文件覆盖其他工作。
   操作应用接口的脚本以服务身份运行；主机配置安装/重启只由本轮授权的管理身份执行。
2. 制品：原/候选配置、绑定和独立key各自计算SHA-256，经
   `dev-host cp <目标> <本地制品> --dest <私有暂存路径> --artifact-sha256 <摘要>` 传输。
   暂存目录0700、文件0600且属于服务身份；在服务venv中用 `GatewayServerSettings.from_env().validate()`
   预检完整候选，断言运维地址非空、consumer共享地址/逻辑执行环境/M3项为空。
   安装前比较原件摘要；以同目录候选原子替换，按授权重启Gateway并检查health。失败恢复原配置。
3. Dify保存：先在本次一次性app演练完整body的 `POST /console/api/apps/<id>/model-config`，
   export回读业务配置相等；现役app只添加新provider的两个条目，旧字段逐项核对未变。
   `DELETE /console/api/apps/<id>` 成功是**204空正文**，用HTTP状态判断并GET404回读，
   不交给必须JSON正文的客户端，也不因解析错误再次删除其他对象。
4. 发布：登记provider后经既有 `POST /v1/dify/publish` 发布当前绑定app，
   服务发布key现场读取，回读 `allowed_tools` 及有效版本。
5. 存储凭据验证：`GET /console/api/workspaces/current/tool-provider/api/get?provider=aiops-ops`
   回读schema与掩码credentials，原样交给Dify自己的测试入口：

   ```json
   {"provider_name":"aiops-ops","tool_name":"order_list",
    "credentials":{"auth_type":"api_key_header","api_key_header":"Authorization",
                   "api_key_header_prefix":"bearer","api_key_value":"<回读掩码>"},
    "parameters":{"page":1,"page_size":1},"schema_type":"openapi","schema":"<回读schema字符串>"}
   ```

   credentials必须使用provider回读的字段字典。`test/pre`按已登记provider
   恢复匹配的掩码值再组装Bearer，结果的`result`是JSON字符串，解析出`result/evidence`并重算摘要。
   一次列表选择其唯一订单，再一次 `charging_info`，不用手填其他订单；SQL/CPMS各一次独立对照。
   不自动重试。HTTP200内有`error`或坏正文均失败，不能以200概括成功。
6. 撤权/恢复：保留新provider条目并关闭新两工具，发布、回读权限并分别验证403；
   然后禁用key绑定（保留行以支持空名单发布），恢复完整原app配置并经同一发布门恢复七工具。
   `POST /console/api/workspaces/current/tool-provider/api/delete` body为 `{"provider":"aiops-ops"}`，
   仅删除本次创建且不再被app使用的provider，回读目录确认不存在。
7. 最后恢复原Gateway配置、授权重启并核对**两份配置**摘要与基线一致，进程/文件开关关闭、health正常、
   新路由返回404 `OPS_TOOLS_DISABLED`。发布版本与审计保留；若撤权时内容已等于原版本，
   恢复发布为`unchanged`，不要求生成额外版本。

Dify 1.17.1 上述存储凭据行为的源码依据：
[`test_api_tool_preview`](https://github.com/langgenius/dify/blob/1.17.1/api/services/tools/api_tools_manage_service.py)
及[工具控制器](https://github.com/langgenius/dify/blob/1.17.1/api/controllers/console/workspace/tool_providers.py)。
版本变化时重新核对，不能从这条证据推断真人身份、模型回答或全环境零调用。
