# 环境接入流水线：阶段 → 文件索引

**读者**：要改「环境接入/候选」流程任一步骤的工程师。**用途**：全部环境接入与候选发布模块已按 ADR-0339 垂直收敛至 `src/aiops_diagnostics/onboarding/` 并保持严格无环分层；本表按阶段列出对应模块。

> 按 ADR-0339 收敛后的源码结构维护；文件增删时同步本表。每格一句话摘自模块职责，细节以源码为准。

| 阶段 | 核心模块（均位于 `src/aiops_diagnostics/onboarding/`，注明包名者除外） |
|---|---|
| 共享叶子上下文 | `context.py`（路径解析、身份元组、知识绑定摘要、`COLD_ACTION`、`submit_cold`）、`registry.py`（环境/租户登记）、`artifacts.py`（应用包与安装器只读成员校验）、`remote_scripts.py`（受控远端脚本）、`business_environments.py`（业务环境端点清单）、`execution_adoption.py`（执行环境迁移） |
| 目录草稿与冻结计划 | `drafts.py`（草稿账本，保存≠发布）、`plans.py`（草稿版本+受控引用构成冻结计划） |
| 现场预检与任务账本 | `tasks.py`（任务持久化/授权/租约）、`task_ssh_runner.py`（SSH 单次远程执行器，原 `environment_task_worker.py`） |
| 候选投影与运行时 | `projection.py`（草稿+模板生成停用候选；投影不安装）、`runtime.py`（已准备资源的私有运行工件） |
| 候选准备 | `candidate_preparation.py`（停用候选准备）、`operator_prep.py`（管家候选应用）、`consumer_prep.py`（消费者候选模板）、`knowledge.py`+`knowledge_connection.py`（候选知识准备与连接）、`candidate_knowledge.py`（候选知识凭据与 `connection_binding`） |
| 安装与登记 | `installation.py`（停用候选的持久安装协调）、`registration.py`（已安装候选接入登记事务） |
| 业务验证与试运行 | `validation.py`（已登记候选三业务验证）、`validation_ssh_runner.py`（验证 SSH 执行器，原 `environment_validation_worker.py`）、`business.py`（目标本机业务验证）、`capacity.py`（运行采样）、`readiness.py`（发布前事实检查）、`trial_runtime.py`（受管管家试运行叶子原语）、`trial.py`（`/v1/internal/environment-trial` 内部路由）、`release_trial.py`（启用前试跑）、`consumer_trial.py`（控制端消费试运行）、`candidate_trial.py`（候选回调 `ect_` 令牌）；注：控制台草稿试跑另在 `consumer/consumer_trial.py` 与 `dify/dify_{workflow_,}trial.py` |
| 正式发布 | `publication.py`（发布协调）、`publication_ssh_runner.py`（发布 SSH 执行器，原 `environment_publication_worker.py`）、`release.py`（发布记录，真值在 Agent 版本与 route CAS；复用 `publication/route_advancement.py`、`consumer/consumer_publication.py` 与 `execution/node_publication.py`） |
| 一键编排与总览 | `orchestration.py`（一键租户开通编排，串联准备→安装→登记→验证）、`overview.py`（只读运行总览） |
| 管理面路由与 CLI | `management.py`（`/v1/dify/management/environment-*`、`/v1/internal/environment-tasks` 及跨阶段 `installation_claim` 派发，经 `dify/self_service_api.py` 挂载）、`admin/onboarding.py`（`aiops-admin environment-*` 子命令） |

## 读法

- 依赖方向严格由上到下单向无环，由 `tests/test_readme_architecture.py`（同时检查绝对导入、相对导入与函数内延迟导入）永久守卫。
- `*_ssh_runner.py` 是发起机上的单次执行体，经 `dev-host exec`（SSH）访问受管目标，不是常驻 daemon；真正在受管主机上跑的是对应的 `*-dispatch` 命令（受管侧固定以 `/opt/aiops-41/` 为部署根，见 `onboarding/task_ssh_runner.py`）。
- 控制台草稿试跑（`dify/dify_trial.py`、`consumer/consumer_trial.py`）与停用目标候选演练（`onboarding/trial*.py`、`onboarding/candidate_trial.py`、`onboarding/release_trial.py`）已通过包边界清晰分离。
