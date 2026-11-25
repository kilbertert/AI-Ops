# 335. 环境与租户管理页总览聚合与运行节点可见性

状态：已采纳。

## 背景

[2026-11-16 手工环境接入与一键开通规划](../architecture/2026-11-16-manual-environment-onboarding.md)（§7）要求在 Dify 主导航「环境与租户」独立管理页内集中呈现四个清晰信息区：**环境概览与连接配置、租户与能力总览、运行节点与工具、开通记录与操作**，避免管理员再跨控制台或命令行排查节点心跳、工具兼容、工作区及应用版本。

此前已交付的环境页聚焦草稿编辑与分步任务提交，存在两类可观察信息缺口：
1. **缺少节点状态面**：控制环境 `execution_node_capabilities` 实时心跳（节点在线状态、协议版本、`operator_tools.json` 摘要兼容与可用工具清单）与远端环境的受管任务点时回执（实测版本、主机名、服务身份与安装态）均未在页面展示；
2. **缺少环境与租户结构化总览**：工作区、双端应用及正式版本号、知识空间和三项能力技术验证结果分散在各条原始任务卡片中，管理员无法按租户一屏核对已开通与待验收边界。

## 决定

1. **新增只读总览聚合模块 `dify/environment_overview.py` 与受控端点 `GET /v1/dify/management/environment-overview`**：
   - 严格复用 `environment_management._access()` 鉴权（正常公司会话 + schema2 目录 + 显式 `managers` 范围），所有环境、草稿、工作区、路由、节点与任务记录均按授权集合严格过滤，越权环境完全不可见；
   - 仅以 SQLite 只读模式读取控制面本地账本（`gateway.db` 与 `<registry>.drafts.db`）与仓内 `operator_tools.json` 声明，不发起任何远端 SSH/HTTP 探测，不写库，也不返回 `config_ref`、`managers`、路径、SSH 目标或凭据正文；
   - **如实区分两类节点事实来源**：
     - 控制环境节点（`source="heartbeat"`）：来自 `execution_node_capabilities` 实时心跳，按既有 `node_capabilities.MAX_AGE`（120 秒）判定 `online`/`stale`，仅在 `tools_sha256` 与本网关 `operator_tools.json` 一致时标 `tools_compatible=true` 并列出 `available_tools`；
     - 远端目标环境节点（`source="task_receipt"`）：汇总最新受管任务（`host-verify` / `site-install` / `candidate-install` / `site-preflight` / `candidate-register` / `environment-publish`）的点时回执，明确标注「受管任务点时回执（非实时心跳）」；
   - **严守业务验收口径**：`candidate-validate` 过期任务按 `expired` 展示（不冒充通过），取消任务按 `unverified` 展示，且所有租户固定返回 `accuracy_confirmed=false`，不把技术验证完成推导为人工业务准确性验收通过。
2. **同步扩展 `GET /v1/dify/management/environment-drafts` 与同源桥 `environment-overview` 动作**：
   - `GET /v1/dify/management/environment-drafts` 附带同授权计算出的 `overview` 字段，使页面首次载入一次请求即可获取草稿与总览，且兼容仅返回既有四字段的旧后端或测试替身；
   - 同源控制台桥 `self_service_bridge.py` 登记只读动作 `environment-overview -> GET /v1/dify/management/environment-overview`，保留既有 Origin/CSRF/Dify 角色与额外字段拒绝校验，不增加发布桥子集包的额外模块依赖。
3. **升级 Dify `/aiops/environments` 页面为四区信息架构**：
   - 顶部增设「环境总览」汇总表，详情区清晰划分为「连接配置与预检计划」「租户与能力总览」「运行节点与工具」「开通记录与操作」四区，并支持按需点击「刷新运行状态」单独拉取最新心跳与回执；
   - 完整保留既有表单控件、草稿版本冲突、未保存离开确认及任务分步按钮契约，确保零破坏性回归。

## 备选方案

- **页面每次载入额外并发请求 `environment-overview`**：会增加一次首屏往返，并在升级过渡期或旧同源桥未就绪时引发首屏报错；将 `overview` 随 `environment-drafts` 一并返回，同时保留独立 `environment-overview` 供按需刷新与直调，兼顾低延迟与平滑滚动升级。
- **在总览接口中实时发 HTTP/SSH 探测远端节点在线性**：会把只读页面查询耦合进慢链路和远端故障放大面，且违背 Gateway 不持 SSH 私钥的既有边界；远端环境坚持使用受管任务持久点时回执，控制环境使用真实节点心跳表。

## 影响与恢复

- 全部改动均为增量只读视图与前端信息架构升级，不改变数据库 schema、业务路由、鉴权放行条件或既有写动作契约；
- 若需回退，可直接恢复上一版 Gateway、控制台桥与 Dify Web 镜像，无持久化迁移残留。
