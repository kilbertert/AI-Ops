# 配置参考：环境变量与 Settings 类的归属

**读者**：需要确认"某个环境变量喂给哪个进程"的工程师。**用途**：配置面按进程分侧，
同一变量名不会跨进程生效；改动配置键前先用本表定位消费方。
权威键清单是根目录 `.env.example`（随代码评审更新），本表是它的索引导航，不逐键重复。

## 进程与配置类对照

| 进程/入口 | 主配置类（文件） | 变量前缀 | 说明 |
|---|---|---|---|
| `aiops-gateway serve`（FastAPI 网关） | `GatewayServerSettings`（`gateway/gateway_config.py` + 分组视图 `gateway/gateway_config_groups.py`） | `AIOPS_GATEWAY_*` | 网关全部对外与内部路由；按 `http` / `caller_auth` / `dify` / `knowledge` / `onboarding` 五组强类型子配置分段校验，并通过 `load_data_sources()` 衔接 `DataSourcesConfig`（`common/config.py::Settings`） |
| `aiops-tdengine-proxy` | `ProxySettings`（`tdengine_proxy.py`） | `AIOPS_TD_PROXY_*` | 独立 `ThreadingHTTPServer` 只读 TDengine 代理；该进程键**不在 `.env.example`**，按其 systemd 单元与本表管理 |
| 登录代理（`services/dify-login-proxy/dify_login_proxy/server.py`，经 `ops/dify-login-proxy` 部署） | `LoginProxySettings`（同文件） | `AIOPS_LOGIN_PROXY_*` | 公司账号进 Dify 控制台的唯一入口；另读 `AIOPS_LOGIN_PROXY_ENVIRONMENT*` |
| 控制台桥（`services/dify-console-bridge/dify_console_bridge/bridge.py`，经 `ops/dify-console-bridge` 部署） | `BridgeSettings`（同文件） | `AIOPS_CONSOLE_BRIDGE_*`（优先读 `AIOPS_CONSOLE_BRIDGE_PUBLISH_API_KEY`/`AIOPS_CONSOLE_BRIDGE_TRIAL_API_KEY`，兼容旧部署的 `AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`/`AIOPS_GATEWAY_DIFY_TRIAL_API_KEY`） | `__aiops/*` 浏览器回调，转发网关；新部署统一使用 `AIOPS_CONSOLE_BRIDGE_*` 前缀，过渡/回滚窗口内须在 `bridge.env` 保留旧 `AIOPS_GATEWAY_DIFY_*` 键直至不再回退旧版桥制品 |
| `aiops-admin` 及诊断/数据路径 | `Settings` / `DataSourcesConfig`（`common/config.py`，组合 `MySQLSettings`/`TDengineSettings`/`RedisSettings`/`UpmsSettings`/`SSHSettings`/`SafetySettings`/`AgentSettings`） | `AIOPS_*`（无前缀层） | `AIOPS_CONFIG_FILE`/`AIOPS_CONFIG_HOME`/`AIOPS_HOME` 定位配置与数据根；网关内可通过 `GatewayServerSettings.load_data_sources()` 读取 |

## 嵌套 Settings：不直接读环境变量

以下类由调用方用主配置的字段构造（如 `GatewayServerSettings.dify_console_base_url`
→ `ConsoleApiSettings`），它们自身不读环境：

| 类 | 定义处 | 由谁构造（例） |
|---|---|---|
| `ConsoleApiSettings` | `dify/dify_console_admin.py` | 网关字段 `dify_console_*` → `environment_management`、`self_service_prepare`、`admin_cli` |
| `InnerApiSettings` | `dify/dify_inner_api.py` | 网关字段 `dify_inner_api_*`；登录代理字段 `inner_api_*` |
| `CompanyAuthSettings` | `services/dify-login-proxy/dify_login_proxy/company.py` | `LoginProxySettings`（`AIOPS_LOGIN_PROXY_COMPANY_*`） |
| `DifyConsoleSettings` | `services/dify-login-proxy/dify_login_proxy/provision.py` | `LoginProxySettings`（`AIOPS_LOGIN_PROXY_DIFY_*`） |
| `ThirdSessionSettings` | `identity/third_session_auth.py` | `onboarding/candidate_trial.py` 等（网关字段 `third_session_*`） |
| `CompanyTokenSettings` | `identity/company_token_auth.py` | 网关字段 `company_token_*` |
| `IntrospectionSettings` | `identity/caller_auth.py` | 网关字段 `introspection_*` |
| `ReadySettings` | `dify/dify_workspace_ready.py` | 网关字段 `dify_console_*`/`dify_workspace_*` |

## 读法约定

- 找"某变量在哪生效"：先按前缀定进程，再在对应 Settings 类里查字段名；`.env.example`
  中的注释按同一分侧组织。
- 两套配置根并存：`GatewayServerSettings`（网关 HTTP 侧）与
  `common.config.Settings`（诊断/CLI 侧，dotenv+文件合并）。网关进程内后者经
  `GatewayRuntime.from_settings` 按 `server_config_file` 加载——`create_gateway_app`
  的 `runtime` 参数接收的是运行时对象而非 `Settings`，两者不要互替。
- 环境注入的运行模式（不产生 `--help` 可见开关）：`M2LIVE_ISOLATION_ROOT` 等
  `M2LIVE_*`（`gateway/isolation_guard.py`）、`AIOPS_GATEWAY_DIFY_DEBUG_*`
  （`dify/dify_debug_identity.py`）。

本表按 2026-11-23 源码快照整理；再生成：`grep -rn 'class \w*Settings' src/` 与
`grep -oh '"AIOPS_[A-Z_]*"' src/aiops_diagnostics/<file>`。
