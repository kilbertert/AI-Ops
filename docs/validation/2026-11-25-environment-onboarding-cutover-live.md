# 41 唯一运行目录切换与 36 配套上线现场回执（2026-11-25）

- **适用版本**：`origin/main` 合并修订 `d871c40cfe7ef0f6262a96a9f3c2efb577c9158a`（含 PR #1700 ADR-0332、PR #1701 ADR-0333、PR #1715 ADR-0335/D50 与 PR #1717 ADR-0336/D51）。
- **适用范围**：41 控制面与唯一运行目录切换（`production-41 -> 41`）、36 配套组件升级（`kb-service --scoped-account`、两 41 租户命名空间重键、`aiops-dify-login-proxy`、`aiops-dify-console-bridge`、`deploy-web-1`）。
- **未触碰边界**：不改公司仓库、公司服务、公司业务库行与 `124`；`95` 保持 `enabled: false` 与双租户停用（路由 0），待后端精确撤销回执后再评估激活窗口。

## 一、已合并与已部署版本核对

| 组件 / 主机 | 已部署版本 / 制品摘要 | 核对结果 |
|---|---|---|
| 41 `aiops-gateway-41.service` | `1.40.4+d871c40cfe7e`（标准 CD run `45596683124`） | `/health` 200，`business_mutations: disabled`，无启动错误 |
| 36 `kb-service`（`/opt/ragflow-kb/kb-service/app.py`） | `--scoped-account` 补丁，SHA-256 `423918601a3bd74416e662b67c415ef28e7c794f69bf45389f31d9ca1477ac3e` | `healthz` `{"status":"ok"}`，新增 `GET /kb/tenants/current` 路由生效 |
| 36 `aiops-dify-login-proxy.service` | `d871c40c` 完整归档（`aiopslogin` 身份） | SQLite 一致副本预检通过（3 表行数一致、`quick_check=ok`、`fd_delta=0`）；已挂接只读同步目录副本（`0700/0600`）；明文登录 400、验证码 200 `image/jpeg`、匿名退出 403、`/__aiops/login/environments` 200 |
| 36 `aiops-dify-console-bridge.service` | `d871c40cfe7ef0f6262a96a9f3c2efb577c9158a` 合并制品 | 21 项 `SHA256SUMS` 校验通过、`python3 -S` 仅标准库导入通过、`nginx -t` 通过、6 路匿名 POST 全部 403 |
| 36 `deploy-web-1`（`0ccaf60c8f6d`） | `aiops/dify-web-patched:onboarding-d871c40c`，OCI Config `sha256:4a5e9c0f9c41e839e880e71335bd7b4cb08dd1e24506c656644c6d79b23b710b` | `deploy-api-1`、`deploy-worker-1`、`deploy-nginx-1` 容器 ID 未变；`app-paths-manifest.json` 含 `/aiops/environments`，产物含 `environment-overview` 与 `onboard_plan`；`/signin=200`、`/aiops/environments=307`、`/__aiops/manage` 匿名 `403` |

## 二、41 唯一运行目录切换与 36 KB 命名空间重键结果

1. **停机冷备与零在飞确认**：切换窗口内先停 41 `aiops-gateway-41.service` 与 36 `aiops-dify-login-proxy.service`，确认 41 `agent_runs` 与 `environment_tasks` 零在飞后完成所有配置文件与 SQLite 私有冷备（`0600`）。
2. **36 外部 KB 命名空间重键**：通过 `deploy/kb-service/migrate_tenant_namespace.py --backup` 对 41 两个现役租户（Ulink、优链充）执行独占备份与重键，均返回 `{"state": "migrated", "changed": true}`；切换后 `GET /kb/tenants/current` 对两个规范环境作用域命名空间返回 HTTP 200，对旧裸租户编号返回 HTTP 404。
3. **41 执行环境单事务迁移**：
   - 5 份自有 JSON 配置（`agent-runs.json`、`node-targets.json`、`publication-targets.json`、`content.json`、`profiles.json`）均由 `"production-41"` 切为 `"41"` 并保留原有 `0600` 属主权限；
   - 执行 `aiops-admin environment-adopt-execution --from production-41 --to 41 --verify-bindings --backup <独占0600清单>` 单事务成功：5 条现役路由迁至 `41`（2 条消费者版本单调克隆新不可变快照）、11 条 `operator_apps` 迁入 `41`（2 条空串孤儿行保留不动）、13 条 `consumer_knowledge_grants` 保撤复制（8 有效 + 5 撤销未复活）、97 条现役管家正式会话迁入 `41`（69 条预览与 2 条退役会话跳过不动）；5 份 `config_refs` 核验均为 `source_refs=0`。
4. **唯一运行目录启用与心跳/总览复核**：
   - 41 中央 `registry.json` 经 `aiops-admin environment-registry apply` 推进至 `revision: 7`（`41` 与两租户启用，`95` 与两租户保持 `enabled: false`）；
   - `/etc/aiops-41/gateway.env` 切换为 `AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=41` 与 `AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE`，移除旧 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE`；
   - 服务拉起后 `execution_node_capabilities` 自动在 `environment='41'` 收到 `node-41` 实时心跳（`status: online`，11 项工具，零重启 Snail-AI 容器）；
   - `EnvironmentRegistry` 实测放行 `41` 两现役租户与平台控制台入口，拒绝未知租户及 `95` 全部租户（`TENANT_ENVIRONMENT_NOT_ENABLED`）；
   - 真实 `environment-overview` 回读（`9,909 B`）完整区分 `41`（`active` + 实时心跳）、`95`（`disabled` + 点时任务回执）与 `124`（`draft_only`）。

## 三、开发期验证与未宣称边界

- **自动化与本地实跑证明**：PR #1715 与 PR #1717 各自完成独立对抗审查、Devin Review 全量缺陷修复、Chromium Playwright 真实浏览器回归（32/32）、一次性数据根 `verify-aiops-gateway`（8/8 真实 HTTP 鉴权与退役路由检查）及全量 CI 绿灯。
- **本回执明确不宣称的内容**：
  1. 尚未执行线上真实人工公司登录后的 `/aiops/environments` 页面交互点击、一键开通页面实跑及切换后的真实业务会话抽测；
  2. 不证明业务准确率已获工程师或人工确认；
  3. 两真实环境生效停用与完整权限验收、`95` 撤销回执后的激活窗口普通入口与追问、原生 Android APK（`#1353/#1422`）及同租户第二真实用户隔离仍未闭环，继续在主索引与验收矩阵跟踪。
