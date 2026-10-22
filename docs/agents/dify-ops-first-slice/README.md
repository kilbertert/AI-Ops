# Dify 租户运维首批工具

规格 #934 / 子任务 #935。首批只读订单列表与当前状态，不做故障归因或修复。
新路由 `POST /v1/dify/ops/{tenant_id}/order_list`、`charging_info` 不接受个人登录会话，
只认独立服务 key；它可读登记租户数据，不能直接供 consumer/self 使用。

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

`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 新格式为逗号分隔 `tenant:https://business/base`，
同地址的多个租户分别登记。无对应行的新工具返回 `TENANT_ENDPOINT_UNRESOLVED`。
历史单一 URL 对旧充电状态调用仍有效，但不为新工具提供兜底。修改配置经正常部署生效。
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
M3的 `AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE`、`AIOPS_GATEWAY_EXEC_MANIFEST_FILE`、
`AIOPS_GATEWAY_EXEC_CONFIG_REQUIRED` 也只核对是否配置。

源码用 `hashlib.sha256(Path(<部署目录>/src/aiops_diagnostics/<文件>).read_bytes()).hexdigest()`，
与合并提交的同文件字节独立比较；`__init__.py` 含部署版本戳，应按部署脚本生成的制品比较。
仅健康与摘要一致证明部署身份，不证明provider登记、Dify模型循环或真实业务结果。
