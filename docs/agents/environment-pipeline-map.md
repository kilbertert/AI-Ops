# 环境接入流水线：阶段 → 文件索引

**读者**：要改「环境接入/候选」流程任一步骤的工程师。**用途**：全部环境接入与候选发布模块已按 ADR-0339 垂直收敛至 `src/aiops_diagnostics/onboarding/` 并保持严格无环分层；本表按阶段列出对应模块。

> 按 ADR-0339 收敛后的源码结构维护；文件增删时同步本表。每格一句话摘自模块职责，细节以源码为准。

| 阶段 | 核心模块（均位于 `src/aiops_diagnostics/onboarding/`，注明包名者除外） |
|---|---|
| 共享叶子上下文 | `context.py`（路径解析、身份元组、知识绑定摘要、`COLD_ACTION`、`submit_cold`）、`registry.py`（环境/租户登记）、`artifacts.py`（应用包与安装器只读成员校验）、`data_preflight.py`（数据面只读权限/索引/时序通路全量预检与白名单修复规划，ADR-0358）、`remote_scripts.py`（受控远端脚本）、`business_environments.py`（业务环境端点清单）、`execution_adoption.py` + `execution_adoption_restore.py`（执行环境迁移预演与冻结备份 CAS 恢复） |
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

## 易混淆模块辨析速查

新接手工程师常被多处相近命名迷惑；下表按“职责 + 调用位置”一次性区分三组高频易混模块：

| 易混分组 | 具体模块 | 职责与边界 | 何时修改这里 |
|---|---|---|---|
| **试运行（Trial）** | `dify/dify_trial.py`、`dify/dify_workflow_trial.py`、`consumer/consumer_trial.py` | 控制台草稿期在线试跑：未上线前由配置人员在 Dify/自服务侧发起的单次调试 | 改控制台草稿试跑请求、Workflow 调试或异步试跑状态 |
| | `onboarding/trial_runtime.py`、`onboarding/trial.py` | 受管目标冷站/停用候选端试跑执行与 `/v1/internal/environment-trial` 内部路由 | 改受管机端冷站试跑原语或内部回调鉴权 |
| | `onboarding/candidate_trial.py`、`onboarding/consumer_trial.py`、`onboarding/release_trial.py` | 上线闸口前候选验证：`candidate_trial` 管 `ect_` 回调令牌；`consumer_trial` 管消费摘要与候选演练；`release_trial` 管启用前最后一次试跑 | 改候选准备/启用前的试跑门禁与签名令牌 |
| **知识（Knowledge）** | `knowledge/*.py`（`knowledge_delegation.py`、`knowledge_retrieval.py`、`knowledge_sources.py` 等） | 生产运行期知识检索、租户授权过滤与委派查询契约 | 改线上问答检索、知识库归属校验与委托协议 |
| | `dify/dify_knowledge_*.py`、`dify/knowledge_connection.py` | Dify 数据集注册/迁移及管理端知识源连接接口 | 改 Dify 知识数据集同步或连接凭据桥接 |
| | `onboarding/knowledge*.py`（`knowledge.py`、`knowledge_connection.py`、`candidate_knowledge.py`） | 环境接入候选阶段的知识连接创建与知识准备任务（尚未影响生效目录） | 改新建/冷启环境时的知识连接编排 |
| **验证与发布（Release Chain）** | `readiness.py`（检查）、`business.py`（目标机自检）、`validation.py`（候选三验证编排） | 发布前逐层门禁：由原子探测到候选业务三项验证，均不修改正式准入 | 改发布前检查项、目标机业务验证条件 |
| | `publication.py`（协调）、`release.py`（不可变记录）、`execution_adoption*.py`（历史环境键迁移/恢复） | `publication` 驱动中心冻结与目录启用；`release` 写入路由 CAS 与发布快照；`execution_adoption` 仅做一次性环境键接管及 CAS 恢复 | 改正式发布流程、目录激活与历史执行环境迁移 |

