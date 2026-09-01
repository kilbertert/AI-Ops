# AFK 可信交付 QA 计划

## AFK-B10 可信工作流静态门

- 环境：AI-Ops AFK 任务分支。
- 前置：模板 1.1.1 已部署。
- 数据：六条 AFK 变更 workflow。
- 动作：运行 `node .sandcastle/policy-check.mjs workflows`、actionlint、ShellCheck，并与 afk-bootstrap 的受管文件逐字节比较。
- 预期：同仓库 owner gate、可信 controller、候选只读 token、干净 delivery checkout 和 AGENT_PAT fail-closed 全部通过，受管文件无漂移。
- 清理：无。

## AFK-B11 Bundle 状态机回归

- 环境：afk-bootstrap 临时 Git 仓库测试。
- 前置：模板测试 checkout 可用。
- 数据：落后的本地 main、前进的 origin main、合并结果和远端竞态。
- 动作：运行 afk-bootstrap 的 `test/trusted-pr-delivery.sh`。
- 预期：基线被重置、bundle 原样保留提交、远端竞态被拒绝。
- 清理：测试 trap 删除临时仓库。

## AFK-B12 Live canary

- 环境：AI-Ops self-hosted runner。
- 前置：加固 workflow 已合并，runner 与只读 token、AGENT_PAT 在线。
- 数据：仓库所有者创建的一次性 PR。
- 动作：添加 `agent:review` 并检查 workflow、review、标签和交付分支。
- 预期：使用当前 main，通过 controller/candidate/delivery 隔离完成审核且没有 blocked 标签。
- 清理：关闭一次性 PR，删除临时分支和标签。

AFK-B10：已通过，时间 `2026-08-30T03:31:15+08:00`，提交
`7380fe8c12c738c4f365db8b13c758d204ebed90`，Linux x86_64，Python
3.13.13、Node v24.15.0、actionlint 1.7.12、ShellCheck 0.11.0。证据：`uv run
ruff check .`、`uv run ruff format --check .`、`uv run pytest -q`、compileall、
`uv pip check`、policy checker、actionlint、ShellCheck、`git diff --check` 全部通过；
受管文件与模板逐字节一致。

AFK-B11：已通过，复用模板提交 `84e9537c661f676f68951eb3e7480472b91ff728` 的
`bash test/trusted-pr-delivery.sh`，覆盖 stale main、bundle 提交保留和远端竞态拒绝。

AFK-B12：待模板合并后在 AI-Ops 在线 self-hosted runner 执行 owner-authored
`agent:review` canary，保留 workflow URL、review、标签和清理证据后再标记通过。
workflow YAML 不适用复杂度或 mutation 工具；安全状态机由模板动态测试覆盖，本仓负责静态门和部署一致性。

## 受限诊断运行时 QA 计划

## SCP-01 权限上下文解析（有效/无效凭证）

- 环境：AI-Ops 本地开发环境，不透传生产凭据。
- 前置：`--scope-json` 选项可用；UPMS/Dis 为离线 fake。
- 数据：有效平台凭证、空凭证、伪造凭证。
- 动作：对每种凭证构造 `ScopeContext`（`ScopeResolver->ScopeRequest`），断言解析成功或 `ScopeError` 失败闭锁。
- 预期：有效凭证得到 `caller/subject/effective_tenant_id/scope_fingerprint`；空/伪造凭证抛 `SCOPE_ERROR_AUTH_FAILED` 且无数据库调用。
- 清理：无。

## SCP-02 目标主体越权与租户切换

- 环境：AI-Ops 本地，UPMS/Dis 离线 fake。
- 前置：`ScopePolicy` 配置代查权限码与管理角色码。
- 数据：无权限调用者、有代查权限调用者、平台管理员、跨租户目标主体。
- 动作：分别请求 `target_b_user_id`、跨租户 `tenant_id`，断言拒绝或解析。
- 预期：无权限代查 → `delegation_denied`；普通调用者指定他人租户 → `tenant_forbidden`；管理员显式切换租户 → 采用请求租户。
- 清理：无。

## SCP-03 三类数据库受限范围查询

- 环境：离线 fixture + 伪造 MySQL/TDengine/Redis 适配器。
- 前置：`QueryScope` 冻结。
- 数据：允许站点/设备/租户内外的订单、设备、报文与 Stream 消息。
- 动作：执行 MySQL 订单/占位费/设备、TDengine 枪属性/报文、Redis Stream 检查，断言过滤条件与结果。
- 预期：范围外数据不返回；空站点短路；越权设备拒绝且不发 TDengine 请求；Redis 只统计租户归属可验证消息；无法验证归属不返回原始正文。
- 清理：无。

## SCP-04 UPMS/Dis 不可用与失败语义

- 环境：离线 fake 目录与 Dis 传输。
- 前置：UPMS/Dis 抛 `URLError` / HTTP 401 / 畸形响应。
- 数据：三种失败输入。
- 动作：解析 `ScopeContext` / 代查目标站点归属，断言错误码。
- 预期：均 fail closed 且错误码区分 `upms_unavailable` / `auth_failed` / `dis_config_missing`；不误报为“空业务数据”。
- 清理：无。

## SCP-05 CLI 受权限诊断入口

- 环境：AI-Ops CLI，Fake `_ssh_tunnel`。
- 前置：`--scope-json` 可用。
- 数据：合法 `QueryScope` JSON、非法 tenant_id。
- 动作：`aiops diagnose --mode deterministic --scope-json '{"tenant_id":"T","site_ids":["S1"],"user_id":null}' ...`；对非法 JSON 断言 `BadParameter`。
- 预期：合法 scope 走 `scoped_live_sources`（受限 MySQL/TDengine/Redis）；非法 scope 被拒绝；无需真实数据库即可验证接线。
- 清理：无。

证据要求：每项记录提交/构建身份、环境、时间戳与日志或报告制品；未连接真实 UPMS/Dis/生产库，fixture 与 fake 不能冒充真实环境验收。
