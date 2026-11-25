# 336. 一键租户开通编排与同摘要计划续签

状态：已采纳。

## 背景

[2026-11-16 手工环境接入与一键开通规划](../architecture/2026-11-16-manual-environment-onboarding.md)（§7.3、§8 P4）要求管理员在 Dify「环境与租户」页面完成「保存草稿 → 连接与权限预检 → 查看变更计划 → 一键开通 → 查看验收结果」的连续体验，同时把正式发布启用保留为人工复核后的显式确认门禁。

此前系统已具备完整的受控原子任务（`candidate-prepare`、`candidate-install`、`candidate-register`、`candidate-validate`、`environment-publish`），但在实际操作中仍需管理员手工衔接 4 次点击提交，且当整条开通链路耗时超过 15 分钟计划窗口时，即使草稿、目录与制品完全未变，管理员也必须人工重新点击预检再发起下一阶段。

## 决定

1. **新增持久编排协调模块 `dify/environment_onboarding.py`（动作 `tenant-onboard`）**：
   - 通过既有 `POST /v1/dify/management/environment-tasks`（同源桥 `environment-task` 动作）接受 `onboard_plan`、`tenant_id`、`source_app_id`、`source_version` 及可选 `initial_installation: true`、`case_ref` + `capabilities`；
   - 沿既有 `advance_candidates` worker 唤醒推进，**不引入新的守护进程或执行引擎**；
   - 严格串联既有原子阶段 `candidate-prepare → candidate-install → candidate-register → [可选 candidate-validate]`，每阶段仍由既有 `submit()` 与独立租约/互斥门禁执行；
   - 提交前先核验 `environment_installation_file` 与目标节点绑定，缺失即快败；同输入幂等复用同一任务编号，同租户并发在飞拒绝（`ENVIRONMENT_TASK_BUSY`）；子阶段遇临时环境锁忙（`ENVIRONMENT_TASK_BUSY`）时保持 `waiting` 等待下轮自然续跑。
2. **同摘要计划窗受控续签**：
   - 在 `environment_plans.preflight()` 增加可选 `min_remaining_seconds` 参数（默认 `0`，兼容全部既有调用）；
   - `tenant-onboard` 仅在进入下一阶段且当前授权、草稿、目录、受控引用、依赖绑定与原始冻结计划摘要 **完全逐比特一致** 时调用 `_renew_same_digest_plan()`（至少保留 300 秒余量）；任何草稿/目录/制品漂移或撤权立即按 `ENVIRONMENT_TASK_PLAN_EXPIRED` / `ENVIRONMENT_MANAGEMENT_FORBIDDEN` 阻塞，绝不静默执行漂移输入；
   - `environment_installation.submit()` 新增可选 `plan_id` 参数以接受同摘要续签计划（不传时 100% 保持原行为）；`environment_installation.current()` 对已 `observed` 安装的下游复核不再因旧墙钟窗口误拒，但继续强制零漂移与绑定核验。
3. **保留正式启用的显式人工确认与安全取消边界**：
   - `tenant-onboard` **绝不自动触发 `environment-publish`**；仅在全部三项能力技术验证通过时标 `ready_to_publish=true` 并在页面展示需人工二次确认的「确认发布并开通」入口，同时固定标注 `business_accuracy="unconfirmed"`；
   - 取消 `tenant-onboard` 在安全边界级联取消未完成的子任务（包括嵌套的 `candidate-prepare` 活跃子任务），保留已 `observed` 资源且不中断已在飞的远端对账任务。
4. **Dify `/aiops/environments` 页面收敛**：
   - 在冻结计划区提供「一键开通（准备 → 安装 → 登记 → 可选技术验证）」主流程卡片，并保留原有分步按钮与既有浏览器测试契约完全兼容。

## 备选方案

- **由前端定时器自动串联多次 POST 提交**：浏览器关闭或网络断线会立即中断流程，且可能因多标签页重复点击引发竞态；改由服务端持久父任务沿既有 `advance_candidates` 推进，断线刷新换设备均可恢复。
- **直接放宽 15 分钟计划窗口到数小时**：会拉长未察觉漂移或撤权的暴露窗口；改为仅在阶段切换时重核完整授权与 SHA-256 输入摘要一致后才续签新计划窗，兼顾长链路可用性与零漂移底线。

## 影响与恢复

- 全部改动复用既有 `environment_tasks` 与 `environment_plans` 表结构，无数据库 schema 变更；
- 原单步准备/安装/登记/验证/发布接口与 UI 按钮保持兼容，回退只需恢复上一版制品与前端镜像，无数据迁移残留。
