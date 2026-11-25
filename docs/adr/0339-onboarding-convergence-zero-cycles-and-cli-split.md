# ADR-0339: 收敛环境接入包 `onboarding/`、实现全包零循环导入并按域拆分 `admin/`

Status: Accepted
Date: 2026-11-25

## 背景

完成 Gateway 路由拆分、配置分组、消费侧整合与 Dify 旁路服务隔离后，代码库仍残留三项显著认知负担：

1. **环境接入全流程被按技术侧横向劈散在四个包里**：原 34 个 `environment_*` / 候选试跑模块分散在 `gateway/`（16 个）、`dify/`（15 个）、`publication/`（1 个）与 `consumer/`（2 个），且远程 SSH 一次性执行脚本以 `*_worker.py` 命名，容易与常驻队列进程混淆。既有单一职责包（`gateway/`、`dify/`、`publication/`、`consumer/`）均无法在不引入跨包互相依赖的前提下完整承载这一独立业务域。
2. **相对导入隐藏了 3 组共 18 个模块的强连通循环（SCC）**：`tests/test_readme_architecture.py::_import_edges()` 此前仅扫描绝对导入，遗漏了三组相对导入循环：
   - `candidate_knowledge` ↔ `environment_knowledge_connection`（2 模块）
   - `environment_management` / `environment_candidate_preparation` / `environment_installation` / `environment_registration` / `environment_validation` / `environment_publication` / `environment_onboarding` / `environment_knowledge`（8 模块）
   - `gateway_api` ↔ `self_service` / `environment_release` 与 `environment_trial` ↔ `environment_{release_trial,capacity,readiness}`（8 模块）
3. **`admin_cli.py` 膨胀至 2,075 行单文件**：30 条 `aiops-admin` 运维命令与共享配置合并辅助混在一起，缺乏清晰的分域入口。

## 决策

1. **新增一级职责包 `src/aiops_diagnostics/onboarding/` 垂直收敛环境接入**：
   - 将全部 34 个环境目录、草稿/计划、任务账本、SSH 远程执行器、候选准备/安装/登记/验证/试跑与正式发布模块迁入 `onboarding/`，不保留旧路径转发壳；
   - 将三份 SSH 远程一次性执行器显式消歧命名为 `task_ssh_runner.py`、`validation_ssh_runner.py`、`publication_ssh_runner.py`；
   - 保留现役 `/v1/dify/trial` 路由与控制台草稿试用路径不破坏，使控制台草稿试用（`dify_trial` / `consumer_trial`）与停用目标候选演练（`onboarding/trial*`、`candidate_trial`、`consumer_trial`、`release_trial`）在包边界上彻底区分；
   - 同步更新 `ops/environments/gateway/{build-installer.sh,probe.py,runtime_check.py,install.md}`、`services/dify-login-proxy/`、`tests/`、`llmdoc/`、`README.md` 与 `docs/architecture/repository-layout.md`。
2. **彻底斩断全部 3 组循环依赖并升级 AST 门禁至绝对+相对导入零环**：
   - 将 `connection_binding(settings)` 纯摘要函数下沉至 `onboarding/candidate_knowledge.py`，消除 SCC 1；
   - 抽出叶子模块 `onboarding/context.py`（`_path`、`_identity`、`knowledge_binding`、`COLD_ACTION`、`submit_cold`）并将跨阶段 `installation_claim` 认领派发上提至顶层 `onboarding/management.py`，消除 SCC 2；
   - 抽出 `publication/route_advancement.py`（`_declared_in_file`、`_key_ref_for`、`advance_route_binding`）切断 `self_service` / `onboarding.release` 对 `gateway_api` 的反向导入，并将 `onboarding/trial.py` 拆为叶子 `onboarding/trial_runtime.py` + 顶层路由 `onboarding/trial.py`，消除 SCC 3；
   - 升级 `tests/test_readme_architecture.py::_import_edges()` 同时解析 `node.level > 0` 相对导入，并新增 `test_import_scan_catches_relative_import_cycles` 回归锁死全包零环。
3. **新增一级职责包 `src/aiops_diagnostics/admin/` 拆分 `admin_cli.py`**：
   - 拆分为 `admin/{common,onboarding,dify,knowledge,maintenance}.py`，`admin_cli.py` 收缩为 31 行薄 Typer 根应用入口（保留 `pyproject.toml` 入口 `aiops_diagnostics.admin_cli:admin_app` 与全部命令名、参数不变）。

## 备选方案（被否）

- **继续把 `environment_*` 保留在 `gateway/` 与 `dify/`，仅靠外部文档表导航**：被否。横跨四包的物理分裂直接诱发了上述 18 个模块的隐性循环，新工程师无法从目录结构建立“配置→准备→安装→登记→验证→发布”的阶段直觉。
- **在旧路径保留转发壳兼容既有测试**：被否。仓库开发共识明确禁止旧路径转发壳，同步更新所有调用方、构建脚本与单测可一步消清旧路径噪音。
- **只把 `admin_cli.py` 放进 `gateway/`**：被否。`aiops-admin` 同时涵盖知识库开户、Dify 同步、环境接入与数据库维护，不属于 Gateway HTTP 运行时，单独建 `admin/` 边界最清晰。

## 后果

- **正面**：新工程师打开 `src/aiops_diagnostics/onboarding/` 即可按单向分层阅读完整环境接入链；打开 `src/aiops_diagnostics/admin/` 可按运维域定位任意 `aiops-admin` 命令；`src/aiops_diagnostics/` 达到绝对+相对导入 0 循环依赖，由 CI 永久守卫。
- **代价与运维注意**：`ops/environments/gateway/build-installer.sh` 已同步改为读取 `src/aiops_diagnostics/onboarding/artifacts.py`，且 `services/dify-login-proxy/` 同步改从 `aiops_diagnostics.onboarding.registry` 导入；后续在 36 主机升级登录代理或在目标机重打固定安装器时需从同一合并提交打包。
