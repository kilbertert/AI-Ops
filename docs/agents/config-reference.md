# 配置参考：环境变量与 Settings 类的归属

**读者**：需要确认"某个环境变量喂给哪个进程"的工程师。**用途**：配置面按进程分侧，
同一变量名不会跨进程生效；改动配置键前先用本表定位消费方。
权威键清单是根目录 `.env.example`（随代码评审更新），本表是它的索引导航，不逐键重复。

## 进程与配置类对照

| 进程/入口 | 主配置类（文件） | 变量前缀 | 说明 |
|---|---|---|---|
| `aiops-gateway serve`（FastAPI 网关） | `GatewayServerSettings`（`gateway/gateway_config.py`） | `AIOPS_GATEWAY_*` | 网关全部对外与内部路由；字段同时驱动下方多个嵌套客户端 Settings |
| `aiops-tdengine-proxy` | `ProxySettings`（`tdengine_proxy.py`） | `AIOPS_TD_PROXY_*` | 独立 `ThreadingHTTPServer` 只读 TDengine 代理 |
| 登录代理（`dify/dify_login_server.py`，经 `ops/dify-login-proxy` 部署） | `LoginProxySettings`（同文件） | `AIOPS_LOGIN_PROXY_*` | 公司账号进 Dify 控制台的唯一入口；另读 `AIOPS_LOGIN_PROXY_ENVIRONMENT*` |
| 控制台桥（`dify/dify_console_publish.py`，经 `ops/dify-console-bridge` 部署） | `BridgeSettings`（同文件） | `AIOPS_CONSOLE_BRIDGE_*` + 少量 `AIOPS_GATEWAY_DIFY_*` | `__aiops/*` 浏览器回调，转发网关 |
| `aiops-admin` 及诊断/数据路径 | `Settings`（`common/config.py`，组合 `MySQLSettings`/`TDengineSettings`/`RedisSettings`/`UpmsSettings`/`DisSettings`/`SSHSettings`/`SafetySettings`/`AgentSettings`） | `AIOPS_*`（无前缀层） | `AIOPS_CONFIG_FILE`/`AIOPS_CONFIG_HOME`/`AIOPS_HOME` 定位配置与数据根 |

## 嵌套 Settings：不直接读环境变量

以下类由调用方用主配置的字段构造（如 `GatewayServerSettings.dify_console_base_url`
→ `ConsoleApiSettings`），它们自身不读环境：

| 类 | 定义处 | 由谁构造（例） |
|---|---|---|
| `ConsoleApiSettings` | `dify/dify_console_admin.py` | 网关字段 `dify_console_*` → `environment_management`、`self_service_prepare`、`admin_cli` |
| `InnerApiSettings` | `dify/dify_inner_api.py` | 网关字段 `dify_inner_api_*`；登录代理字段 `inner_api_*` |
| `CompanyAuthSettings` | `dify/dify_login_company.py` | `LoginProxySettings`（`AIOPS_LOGIN_PROXY_COMPANY_*`） |
| `DifyConsoleSettings` | `dify/dify_login_provision.py` | `LoginProxySettings`（`AIOPS_LOGIN_PROXY_DIFY_*`） |
| `ThirdSessionSettings` | `identity/third_session_auth.py` | `consumer/candidate_trial.py` 等（网关字段 `third_session_*`） |
| `CompanyTokenSettings` | `identity/company_token_auth.py` | 网关字段 `company_token_*` |
| `IntrospectionSettings` | `identity/caller_auth.py` | 网关字段 `introspection_*` |
| `ReadySettings` | `dify/dify_workspace_ready.py` | 网关字段 `dify_console_*`/`dify_workspace_*` |

## 读法约定

- 找"某变量在哪生效"：先按前缀定进程，再在对应 Settings 类里查字段名；`.env.example`
  中的注释按同一分侧组织。
- 两套配置根并存：`common.config.Settings`（诊断/CLI 侧，dotenv+文件合并）与
  `GatewayServerSettings`（网关侧）；`gateway_api` 同时使用两者（`settings` 与 `runtime`
  参数），不要互替。
- 环境注入的运行模式（不产生 `--help` 可见开关）：`M2LIVE_ISOLATION_ROOT` 等
  `M2LIVE_*`（`gateway/isolation_guard.py`）、`AIOPS_GATEWAY_ALLOW_FIXTURES`、
  `AIOPS_GATEWAY_DIFY_DEBUG_*`（`dify/dify_debug_identity.py`）。

本表按 2026-11-23 源码快照整理；再生成：`grep -rn 'class \w*Settings' src/` 与
`grep -oh '"AIOPS_[A-Z_]*"' src/aiops_diagnostics/<file>`。
