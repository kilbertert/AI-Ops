# 环境接入流水线：阶段 → 文件索引

**读者**：要改「环境接入/候选」流程任一步骤的工程师。**用途**：`environment_*` 前缀
横跨 `gateway/`（账本与编排侧）与 `dify/`（Dify 供给侧）两包约 30 个模块，文件名
不带阶段信息；本表是定位索引。分侧规则本身见两包 `__init__.py` 与
`GLOSSARY.md` 的「`environment_*` 代码前缀」条目。

> 按 2026-11-23 源码快照整理；文件增删时同步本表。每格一句话摘自模块 docstring，
> 细节以源码为准。

| 阶段 | 账本/编排侧（gateway/） | 供给侧（dify/ 等） |
|---|---|---|
| 目录草稿 | `environment_drafts.py`：草稿账本，保存≠发布 | — |
| 冻结计划 | `environment_plans.py`：草稿版本+受控引用构成冻结计划 | — |
| 现场预检 | `environment_tasks.py`（任务持久化/授权/租约）、`environment_task_worker.py`（SSH 远程执行端） | — |
| 候选投影 | `environment_projection.py`：草稿+模板生成停用候选；投影不安装 | `environment_runtime.py`：已准备资源的私有运行工件 |
| 候选准备 | — | `environment_candidate_preparation.py`（停用候选准备）、`environment_operator.py`（管家候选应用）、`environment_consumer.py`（消费者候选模板）、`environment_knowledge.py`+`environment_knowledge_connection.py`（候选知识准备）、`candidate_knowledge.py`（候选连接凭据） |
| 安装 | `environment_artifacts.py`：应用包与安装器的只读成员校验 | `environment_installation.py`：停用候选的持久安装协调 |
| 登记 | `environment_registry.py`：环境/租户登记 | `environment_registration.py`：已安装候选接入登记事务 |
| 验证 | `environment_validation_worker.py`（SSH 远程执行端）、`environment_capacity.py`（运行采样）、`environment_business.py`（目标本机业务验证）、`environment_readiness.py`（发布前事实检查） | `environment_validation.py`：已登记候选的实际三业务验证 |
| 试运行 | `environment_trial.py`（受管管家试运行路由）、`environment_release_trial.py`（启用前试跑） | `environment_consumer_trial.py`（控制端执行）、`consumer/candidate_trial.py`（候选回调，`ect_` 令牌）、`consumer/consumer_trial.py`（临时 Chatflow 草稿试跑） |
| 发布 | `environment_publication_worker.py`（SSH 远程执行端） | `dify/environment_publication.py`（发布协调）、`publication/environment_release.py`（发布记录，真值在 Agent 版本与 route CAS）、`consumer/consumer_publication.py`（WF01 发布目标）、`execution/node_publication.py`（节点 SOP→执行清单） |
| 管理面路由 | — | `environment_management.py`：`/v1/dify/management/environment-*` 路由，经 `self_service_api.register` 挂载 |

## 读法

- 找"这一步在哪"：先按阶段定行，再看职责在两侧哪一栏；同名前缀的另一半
  在另一包里。
- 「worker」是发起机上的单次执行体，经 `dev-host exec`（SSH）访问受管目标，
  不是常驻 daemon；真正在受管主机上跑的是对应的 `*-dispatch` 命令
  （受管侧固定以 `/opt/aiops-41/` 为部署根，见 `environment_task_worker.py`）。
- 「trial/试运行」是用草稿或停用候选跑一次；「candidate/候选」是被准备出来
  尚未激活的对象。同名 `*_trial` 文件分处三包是因为它们各是该流程一段的
  端侧，不是重复实现。
- 新模块命名优先写全词义（`business_environment_*`、`candidate_*`），不再新增
  裸 `environment_*`；确需新增时归入上表对应阶段的一栏并同步本表。
