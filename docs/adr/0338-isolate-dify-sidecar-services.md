# ADR-0338: 将 Dify 控制台发布桥与登录代理从 Gateway 包剥离为独立旁路服务

Status: Accepted
Date: 2026-11-25

## 背景

`src/aiops_diagnostics/dify/` 此前同时混放了三套不同进程、不同依赖约束、不同部署制品的代码：

1. **Gateway 内部 Dify 适配器**（运行于 Gateway FastAPI 进程，打包进完整 wheel/Docker 镜像）；
2. **Dify 控制台发布桥**（`dify_console_publish.py` + `self_service_bridge.py` + `console_environments.py` 的 `credentials()`，在 36 主机独立运行，仅允许 Python 标准库依赖，由 `ops/dify-console-bridge/build-bundle.sh` 抽取子集文件打包）；
3. **Dify 公司登录代理**（`dify_login_{proxy,company,provision,service,server}.py` + `console_environments.py` 的 `LoginEnvironments`，在 36 主机以独立 systemd 服务与 `mappings.db` 运行）。

这带来了四项高认知成本与隐性维护陷阱：

- 发布桥无法导入 Gateway 模块，被迫在 `dify_console_publish.py` 内重写一份 `PLATFORM_USER_TYPE` / `PLATFORM_TENANT_ID` 并用专项测试防漂移；同时 `PLATFORM_TENANT_ID` 在 `dify_workspaces.py`（`"platform"`）与 `shortcut_lifecycle.py`（`"__platform__"`）之间同名异义。
- 发布桥既作为库被导入又直接 `python -m` 启动，导致双重模块加载，迫使 `bridge` 通过 `handle(self, sys.modules[__name__])` 把自身模块对象传给 `self_service_bridge`。
- `console_environments.py` 被两个旁路进程混用，部署测试被迫维护 `_LOGIN_PROXY_ONLY_DEFS` 白名单排除登录代理代码。
- Gateway 模块 `self_service_api.py` 反向导入了 `dify_console_publish._request`，登录代理 `server.py` 又跨服务导入 `dify_console_publish.console_session_from_headers`。

## 决策

1. **按真实运行进程边界隔离独立旁路服务**：
   - 将发布桥移至 `services/dify-console-bridge/dify_console_bridge/`（`bridge.py`、`self_service_bridge.py`、`environments.py`、`__main__.py`），启动入口为 `python -m dify_console_bridge`。
   - 将登录代理移至 `services/dify-login-proxy/dify_login_proxy/`（`core.py`、`company.py`、`provision.py`、`service.py`、`server.py`、`environments.py`、`__main__.py`），启动入口为 `python -m dify_login_proxy`。
   - 不在旧 `src/aiops_diagnostics/dify/` 路径保留转发壳；同步更新 `pyproject.toml`（pytest `pythonpath`）、`ops/` 部署脚本与 systemd 单元、`tests/test_repository_layout.py`、`docs/architecture/repository-layout.md` 与 `README.md`。
2. **共享原语下沉至无外部依赖的 `aiops_diagnostics.common`**：
   - 新增 `common/platform_constants.py` 唯一声明 `DIFY_PLATFORM_WORKSPACE_ID = "platform"` 与 `PLATFORM_USER_TYPE = "-1"`；全面移除 `PLATFORM_TENANT_ID` 歧义别名。
   - 将 `ConsoleSession` 与 `console_session_from_headers` 上移至 `common/http_auth.py`，消除登录代理与发布桥之间的交叉导入；`self_service_api.py` 改用 `common.bounded_http.request_json`，实现 `src/aiops_diagnostics/` 对两个独立旁路零反向依赖。
3. **分离 `__main__.py` 入口并拆分 `environments.py`**：
   - `dify_console_bridge/__main__.py` 仅作为 CLI 入口导入 `dify_console_bridge.bridge.main`，`self_service_bridge.py` 直接 `from . import bridge`，彻底删除 `sys.modules[__name__]` 传参技巧。
   - 发布桥仅保留 `dify_console_bridge/environments.py` 的 `credentials()`；登录代理独立持有 `dify_login_proxy/environments.py` 的 `LoginEnvironments`，删除测试中的 `_LOGIN_PROXY_ONLY_DEFS` 豁免。
4. **平滑升级打包与部署脚本**：
   - `ops/dify-console-bridge/build-bundle.sh` 打包 `dify_console_bridge/` 与最小标准库原语子集 `aiops_diagnostics/`（不再包含 `aiops_diagnostics/dify/`）。
   - `ops/dify-console-bridge/deploy.sh` 兼容将旧 `ExecStart`（`-m aiops_diagnostics.dify_console_publish` 或 `-m aiops_diagnostics.dify.dify_console_publish`）原子升级为 `-m dify_console_bridge`，并同时备份/恢复 `aiops_diagnostics` 与 `dify_console_bridge` 目录。

## 备选方案（被否）

- **继续留在 `src/aiops_diagnostics/dify/` 并在旧路径保留转发壳**：被否。仓库开发共识明确禁止旧路径转发壳，且保留在 Gateway 包内无法从目录边界区分“Gateway 进程内代码”与“36 主机仅标准库独立运行进程”，仍需维护常量复制和部署测试排除清单。
- **将两个旁路拆分为完全独立的外部仓库**：被否。发布桥和登录代理与 Gateway 共享 `common/http_auth.py`、`common/bounded_http.py`、`common/platform_constants.py` 及同一套集成回归测试，留在同仓 `services/` 下既明确运行边界，又保留单一 CI 回归门禁。

## 后果

- **正面**：新工程师打开 `services/` 即可看到全部四个独立进程（`execution-node`、`model-proxy`、`dify-console-bridge`、`dify-login-proxy`）；`src/aiops_diagnostics/dify/` 纯粹只包含 Gateway 侧的 Dify 集成代码；发布桥无常量复制、无 `sys.modules` 技巧、无部署白名单例外。
- **代价与运维注意**：36 主机下次升级控制台发布桥与登录代理时，需按 `ops/dify-console-bridge/README.md` 与 `ops/dify-login-proxy/README.md` 使用同修订制品与对应 `ExecStart` 单元同步切换（Gateway CD 不自动覆盖 36 旁路服务）。
