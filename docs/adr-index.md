# 现行架构决策索引（Active ADR Index）

> **给新接手工程师的 1 分钟导读**：
> - 本页列出主仓库 `docs/adr/` **当前生效的 127 份核心架构决策**（按领域归类）。想知道「系统为什么这样设计、哪些方案已被否决」，先看本页对应行再点进具体 ADR。
> - 已被取代、反转、撤回、到期，以及单一 PR / 局部 bugfix / 一次性清理的 **231 份历史与战术 ADR**，已依据 [ADR-0370](adr/0370-archive-superseded-and-tactical-adrs.md) 完整性校验后迁出至私有归档仓库 [`kilbertert/AI-Ops-history-archive`](https://github.com/kilbertert/AI-Ops-history-archive/tree/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr)（归档提交 `4bed22cb`）。
> - 运行现状请读 [当前交付状态](agents/current-delivery-state.md)；Dify 宿主现状请直接看唯一架构入口 [`deploy/dify-36/ARCHITECTURE.md`](../deploy/dify-36/ARCHITECTURE.md)。

---

## Dify @ 36 现行决策速览（防踩坑对照表）

理解 Dify 与 AI-Ops 协同方式时，先用下表区分「当前生产生效」与「已推翻/已退役历史」：

| 主题 | 现行生效真相 | 已推翻 / 已退役历史（请勿沿用） |
|---|---|---|
| **总体拓扑与唯一入口** | [`deploy/dify-36/ARCHITECTURE.md`](../deploy/dify-36/ARCHITECTURE.md)（Nginx → `aiops-dify-edge` + `aiops-dify-web` + Dify API + `inner-api-forward`） | 散落各 `README.md` 中的手写多份运行图与旧 `tar.gz` / systemd 部署指令 |
| **边缘侧边车分发** | [ADR-0367](adr/0367-dify-edge-sidecars-acr-convergence.md)、[ADR-0369](adr/0369-retire-legacy-dify-edge-bundles-and-add-request-tracing.md)：单一共享 ACR `@sha256` 镜像 `aiops-dify-edge`，双容器不同 Linux UID 隔离（`aiopsbridge` / `aiopslogin`），支持 `X-Request-Id` | `build-bundle.sh` / `deploy.sh` / 独立 systemd unit（已在 ADR-0369 删除） |
| **独立管理台 (`/aiops/*`)** | [ADR-0368](adr/0368-decouple-aiops-admin-console-from-dify-web.md)：`services/aiops-console/` 脱离 Dify Next.js，由 `aiops-dify-console-bridge` 同源托管 | 把环境管理工作台嵌进 Dify Next.js 源码补丁编译 |
| **Dify Web 前端扩展与分发** | [ADR-0365](adr/0365-dify-web-acr-oci-distribution.md)、[ADR-0366](adr/0366-retire-nginx-subfilter-js-injection.md)：`deploy/dify-36/web-patches/` 编译入 `aiops-dify-web` ACR `@sha256` 镜像，RAGFlow 卡与发布条均为 React 组件 | Nginx `sub_filter` 运行时野生 JS 脚本注入（已在 ADR-0366 退役） |
| **工作区与登录隔离模型** | 短期演进与目标设计：[ADR-0023](adr/0023-dify-login-proxy-signs-console-sessions.md)（自签会话）、[ADR-0059](adr/0059-one-workspace-per-tenant-per-environment.md) + [ADR-0068](adr/0068-claim-before-create-and-two-kinds-of-failure.md) + [ADR-0072](adr/0072-login-proxy-provisions-per-tenant.md)（按租户×环境分配 workspace） | 早期的单一全局工作区假设（`ADR-0056` 已到期并归档） |
| **环境 95 独立重建实验** | [环境 95 事实清单](agents/env-95-facts.md)：95 已改回接入平台目录，旧 95 重建方案已撤回（见 [环境目录规范](agents/environment-registry.md)） | `ADR-0257`/`0260`/`0262`/`0263`/`0264` 95 专项重建方案（已撤回/归档） |

---

## 一、核心边界、身份与权限模型（15 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0001](adr/0001-read-only-diagnostic-boundary.md) | 诊断一期只读边界与宿主无关执行模型（禁止修改业务订单/配置/重算） | Accepted |
| [ADR-0004](adr/0004-bff-owns-frontend-api-boundary.md) | 小程序 BFF 负责前端业务接口边界，AI-Ops 只服务受托诊断接口 | Accepted |
| [ADR-0006](adr/0006-shortcut-actions-extend-to-in-app-navigation.md) | 快捷动作扩展为意图入口与站内导航目标 | Proposed（局部契约保留） |
| [ADR-0008](adr/0008-adr-0003-deviation-session-direct-read.md) | 会话身份采用共享 Redis 直读偏离（替代 BFF 委托句柄实现） | Accepted |
| [ADR-0009](adr/0009-operator-identity-via-company-oauth2-token.md) | 管家端身份由公司 OAuth2 令牌断言 | Accepted |
| [ADR-0024](adr/0024-publish-authority-by-company-permission.md) | 发布权由公司 UPMS 权限码裁定，发布审计按人记账 | 已采纳 |
| [ADR-0028](adr/0028-company-identity-drives-the-console-and-trial-scope.md) | 公司身份是控制台会话与试运行可见范围的唯一来源 | 已采纳 |
| [ADR-0029](adr/0029-aiops-permission-codes-in-company-upms.md) | AI-Ops 管理与发布权限码注册在公司 UPMS 菜单-角色模型中 | 已采纳 |
| [ADR-0084](adr/0084-admin-roles-come-from-company-permission-codes.md) | 管理面角色直接映射自公司权限码 | 已采纳 |
| [ADR-0139](adr/0139-console-admission-is-per-workspace.md) | 控制台准入按目标 workspace 判定，不按持有权限码笼统放行 | 已采纳 |
| [ADR-0231](adr/0231-capability-source-tiers.md) | 能力声明按来源分档：`required_scope` 只比对不回填，退役任意权限门 | Accepted |
| [ADR-0265](adr/0265-company-gateway-request-admission.md) | 公司网关来源证明先于用户授权校验，重放账本独立保留 | Accepted |
| [ADR-0319](adr/0319-environment-user-ingress-entry-credential-split.md) | 环境用户助手入口按申报入口分流凭据，忽略客户端自报身份头 | 已采纳 |
| [ADR-0320](adr/0320-registry-entry-dispatches-caller-chains.md) | 登记表按申报入口分派本环境凭据链，operator 令牌走本环境 UPMS | 已采纳 |
| [ADR-0332](adr/0332-platform-and-manager-admission-under-environment-registry.md) | 启用运行目录后的平台账号与目录管理员登录准入边界 | 已采纳 |

## 二、Dify @ 36 架构与边缘侧边车（32 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0018](adr/0018-operator-publish-route-shared-key.md) | 运营自助发布走独立写端点 + 共享密钥，作用域限定注册表 | 已采纳 |
| [ADR-0019](adr/0019-tool-allowlist-enforced-in-executor.md) | 工具白名单由 Dify 声明、执行器强制，按提供方与 `operationId` 对齐 | 已采纳（已修订） |
| [ADR-0020](adr/0020-dify-owns-the-zh-master-of-starters.md) | 开场白与预设问题以 Dify 存中文母版，多语言由产物提供 | 已采纳 |
| [ADR-0022](adr/0022-dify-trial-runs-the-same-path.md) | 控制台试运行复用正式执行链路，仅切换输入草稿与作用域 | 已采纳 |
| [ADR-0023](adr/0023-dify-login-proxy-signs-console-sessions.md) | 登录代理用公司账号换取 `SECRET_KEY` 自签 Dify 控制台会话 | 已采纳 |
| [ADR-0026](adr/0026-console-entry-tls-on-own-domain.md) | Dify 控制台入口收口至自有域名 TLS，不经第三方代理 | 已采纳 |
| [ADR-0027](adr/0027-trial-draft-is-read-by-the-gateway.md) | 试运行编排草稿由网关直接读取，作为跨服务契约 | 已采纳 |
| [ADR-0059](adr/0059-one-workspace-per-tenant-per-environment.md) | 目标多工作区模型：一个 `(租户, 环境)` 对应独立 Dify workspace | 已采纳（设计定型） |
| [ADR-0061](adr/0061-dify-account-email-carries-the-environment.md) | Dify 账号邮箱携带环境前缀，隔离两套 UPMS 防撞号串账号 | 已采纳 |
| [ADR-0063](adr/0063-knowledge-binding-is-self-service.md) | 知识库挂接由运营在产品界面完成，非工程师手工填表 | 已采纳 |
| [ADR-0068](adr/0068-claim-before-create-and-two-kinds-of-failure.md) | 创建 workspace 坚持“先本地占位、再远端创建”，失败分两侧处理 | 已采纳 |
| [ADR-0070](adr/0070-inner-api-forwarder-resolves-per-connection.md) | `inner-api` 转发器每次连接现查 `deploy-api-1` 容器地址 | 已采纳 |
| [ADR-0072](adr/0072-login-proxy-provisions-per-tenant.md) | 登录代理按租户开户：解析走网关，join 失败不阻断正常登录 | 已采纳 |
| [ADR-0073](adr/0073-publish-pins-the-initiators-workspace.md) | 发布动作钉住发起人所在 workspace，禁止回退默认值 | 已采纳 |
| [ADR-0077](adr/0077-dify-media-urls-are-absolute-and-agentless.md) | 交付给 Dify 的媒体 URL 必须绝对可达且不绑定特定智能体身份 | 已采纳 |
| [ADR-0085](adr/0085-tenant-switching-lives-in-dify-not-in-a-fork.md) | 运维切租户依靠 Dify 原生成员关系切换，不维护 Dify 分支 fork | 已采纳 |
| [ADR-0096](adr/0096-dify-datasets-are-synced-and-granted-in-two-steps.md) | RAGFlow 知识库同步至 Dify dataset 与权限授予拆为两步 | Accepted |
| [ADR-0122](adr/0122-dataset-sync-happens-when-the-picker-opens.md) | 建库同步触发于“添加知识库”弹窗打开瞬间，无需定时轮询 | Accepted |
| [ADR-0124](adr/0124-chat-dify-route-uses-a-one-shot-data-grant.md) | 聊天可走 Dify 编排，但取数凭据限定为当次当单一次性授权 | 已采纳 |
| [ADR-0127](adr/0127-publish-switches-the-route-and-retract-keeps-the-row.md) | 发布切正式路由，撤回仅改状态不删除绑定记录 | 已采纳 |
| [ADR-0136](adr/0136-console-session-lifetime-is-a-deployment-parameter.md) | 控制台会话寿命由部署参数配置而非写死默认值 | Accepted |
| [ADR-0161](adr/0161-group-dify-integration-package.md) | Dify 集成代码归并至 `dify` 包并与独立旁路服务成套交付 | Accepted |
| [ADR-0162](adr/0162-dify-self-service-owns-managed-operator-apps.md) | 自助开通统一管理管家应用身份，区分平台连接与正式路由 | 已采纳 |
| [ADR-0276](adr/0276-console-session-selects-environment.md) | 控制台后续管理路由使用当前活动登录绑定的环境 | accepted |
| [ADR-0306](adr/0306-formal-scoped-dify-service-ingress.md) | 将既有受限 Dify HTTPS 虚拟主机转正为第二环境服务 API 入口 | Accepted |
| [ADR-0338](adr/0338-isolate-dify-sidecar-services.md) | 从 `aiops_diagnostics` 彻底剥离 `dify-console-bridge` 与 `dify-login-proxy` 为独立服务包 | Accepted |
| [ADR-0365](adr/0365-dify-web-acr-oci-distribution.md) | Dify Web 前端定制通过 `web-patches` 打入公司 ACR `@sha256` OCI 镜像并在 36 滚动升级 | 已采纳 |
| [ADR-0366](adr/0366-retire-nginx-subfilter-js-injection.md) | 退役 Nginx `sub_filter` 野生 JS 注入脚本，前端能力全部收进 React 补丁 | 已采纳 |
| [ADR-0367](adr/0367-dify-edge-sidecars-acr-convergence.md) | 36 双侧边车收敛为单一共享 ACR 镜像 `aiops-dify-edge`（双容器 UID 隔离）与唯一架构入口 | 已采纳 |
| [ADR-0368](adr/0368-decouple-aiops-admin-console-from-dify-web.md) | `/aiops/*` 独立管理工作台迁出 Dify Next.js 至 `services/aiops-console/`，由边缘桥同源托管 | 已采纳 |
| [ADR-0369](adr/0369-retire-legacy-dify-edge-bundles-and-add-request-tracing.md) | 退役旧 `build-bundle.sh` / `deploy.sh` / systemd 资产，补全图标与 `X-Request-Id` 跨跳追踪 | 已采纳 |
| [ADR-0372](adr/0372-single-control-plane-cross-env-dify-management-and-retire-legacy-tenant-setup.md) | 单控制面跨环境 Dify 提示词编辑/发布/回滚（41 经固定密钥 SSH 远端维护客户机 Gateway）并彻底退役旧版 `/apps` 顶部「复制并开通客户 AI 服务」 | 已采纳 |

## 三、Gateway、Schema、执行节点与电池健康（18 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0041](adr/0041-model-egress-is-a-fixed-target.md) | Java 执行节点大模型出口收口至固定代理目标（`model-proxy`） | 已采纳 |
| [ADR-0079](adr/0079-app-entry-and-execution-engine-are-independent.md) | 用户应用入口与后端执行引擎分别解耦绑定 | 已采纳 |
| [ADR-0104](adr/0104-chat-to-node-is-async-and-fails-closed.md) | App 聊天转节点执行采用异步派发与存储关联，失败拒绝回落 | 已采纳 |
| [ADR-0156](adr/0156-public-user-openapi.md) | 用户开放接口规范按 HTTP 方法与路径显式纳入公开 OpenAPI 清单 | Accepted |
| [ADR-0158](adr/0158-retire-python-runtime.md) | 彻底退役 Python Thin Harness 执行器并保留独立运维入口 `aiops-admin` | Accepted |
| [ADR-0181](adr/0181-a-tool-is-a-declaration.md) | 工具边界与能力以静态声明为准，避免散落硬编码判断 | 已采纳 |
| [ADR-0188](adr/0188-the-gateway-may-not-run-ahead-of-the-node.md) | 版本兼容性约束：Gateway 不得先于执行节点契约上线 | 已采纳 |
| [ADR-0212](adr/0212-credential-redaction-has-one-shape.md) | 凭据脱敏规则统一收口，URL userinfo 通过 `urlsplit` 净化 | 已采纳 |
| [ADR-0213](adr/0213-outbound-endpoint-policy-has-one-rule.md) | 出站端点 URL 的 SSRF/安全校验只保留一处权威实现 | 已采纳 |
| [ADR-0230](adr/0230-gateway-setting-source-table.md) | Gateway 运行设置严格按来源表加载，关键配置缺失即 fail-closed | Accepted |
| [ADR-0244](adr/0244-gateway-schema-authority.md) | Gateway 全库结构由单一静态声明（`gateway_schema`）权威治理 | Accepted |
| [ADR-0254](adr/0254-case-exploration-shortcut-retired-everywhere.md) | 「客户案例」快捷指令从 C 端与管家端两侧整体永久退役 | 已采纳 |
| [ADR-0337](adr/0337-operator-ai-drawer-architecture.md) | 充电桩后台 AI 抽屉契约：零改码下载中心对接、附件只读隔离与 SSE 增量流 | 已采纳 |
| [ADR-0339](adr/0339-onboarding-convergence-zero-cycles-and-cli-split.md) | `onboarding/` 收敛、全 Python 包导入图严格无环、`admin/` 按域拆分 | Accepted |
| [ADR-0340](adr/0340-async-jobs-schema-v4-and-docs-archival.md) | `standard_diagnoses` 原子迁移为统一 `async_jobs`（Schema v4） | Accepted（Schema v4 现行） |
| [ADR-0353](adr/0353-battery-health-report-v3-metrics-and-ykc-frames.md) | 电池健康报告 `health-v3` 指标口径、确定性结论与 YKC 协议帧接入 | Accepted |
| [ADR-0360](adr/0360-health-report-tsdata-fallback-and-user-ingress.md) | 健康报告增加通用 tsdata 时序回退、放行环境用户入口与修正图例命名 | 已采纳 |
| [ADR-0362](adr/0362-public-openapi-single-source-user-ingress.md) | 客户环境用户入口直接以公开 OpenAPI 为单一真源自动生成并做门禁对齐 | 已采纳 |

## 四、C 端客服 (WF01) 与知识库（17 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0033](adr/0033-consumer-charging-status.md) | 本人实时充电状态经授权后直读业务 API | 已采纳（尚未实际部署） |
| [ADR-0090](adr/0090-diagnosis-contract-has-no-unsatisfiable-constraints.md) | 诊断交付合同必须可同时满足，消除自相矛盾规则 | 已采纳 |
| [ADR-0095](adr/0095-knowledge-id-is-one-per-library.md) | `knowledge_id` 坚持一库一条、按租户保留 | Accepted |
| [ADR-0120](adr/0120-external-video-goes-in-url.md) | 外部视频播放地址随 `media.url` 原样返回（对接现有前端契约） | Accepted |
| [ADR-0151](adr/0151-consumer-knowledge-node-selection.md) | WF01 知识库选择随节点配置发布，正文仍走单轮授权 | 已采纳（正文部分由 0152 补充） |
| [ADR-0152](adr/0152-knowledge-documents-are-the-live-answer-source.md) | RAGFlow 知识文档作为知识正文与关联媒体的权威实时来源 | 已采纳 |
| [ADR-0153](adr/0153-consumer-language-follows-app.md) | C 端输出语言跟随 App 参数，模型能力与旧关键词路由分离 | 已采纳 |
| [ADR-0182](adr/0182-consumer-answers-lead-with-the-conclusion.md) | C 端状态与账单答复先给确定性结论，再做自然语言解释 | 已采纳 |
| [ADR-0186](adr/0186-consumer-copy-is-a-published-bundle.md) | C 端固定答复措辞作为按租户发布的版本化文案包，数字由代码注入 | 已采纳 |
| [ADR-0187](adr/0187-consumer-chatflow-prompt-writes-the-reply.md) | Chatflow 提示词可写正文表达，但核心数值必须来自本轮受控快照 | 已采纳 |
| [ADR-0189](adr/0189-dual-entry-onboarding-is-a-managed-catalogue.md) | 客户开通落于受管应用目录，两端渠道独立准备与发布 | 已采纳（已修订） |
| [ADR-0232](adr/0232-consumer-agent-conversation.md) | C 端由单 Agent 协同决策，长期会话与短期取数授权生命周期分离 | Accepted |
| [ADR-0246](adr/0246-consumer-order-composition.md) | 消费者订单构成复用确定性账务解析路径交付 | Accepted |
| [ADR-0247](adr/0247-consumer-order-facts-rendered.md) | 客服订单核对事实段由网关代码直接确定性渲染 | Accepted |
| [ADR-0250](adr/0250-natural-replies-and-nonblocking-currency.md) | 恢复 C 端模型自然答复表达，单币种差异不单独阻断管家分析 | Accepted |
| [ADR-0261](adr/0261-knowledge-environment-namespace.md) | 共享 RAGFlow 知识服务的命名空间身份包含所属业务环境 | Accepted |
| [ADR-0302](adr/0302-scoped-knowledge-delegation.md) | 跨环境知识能力按应用与既有授权世代委派 | Accepted |

## 五、管家端 (Operator / CPMS) 与运行追踪（12 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0146](adr/0146-operator-node-tools-use-cpms-api.md) | 管家诊断统一以 CPMS API 工具清单交给 Java 执行节点调用 | 已采纳 |
| [ADR-0163](adr/0163-operator-output-is-a-runtime-contract.md) | 管家输出协议由节点运行时执行约束，最终正文只采信末轮输出 | 已采纳 |
| [ADR-0171](adr/0171-operator-report-answers-the-question-asked.md) | 管家报告须直接回答提问焦点，取证覆盖金额与额定功率字段 | 已采纳 |
| [ADR-0174](adr/0174-capability-is-the-run-type.md) | 能力模型作为诊断 run 的一级类型，订单仅作为可选输入参数之一 | 已采纳（已修订） |
| [ADR-0175](adr/0175-tool-grant-carries-the-request.md) | 工具授权令牌绑定具体的请求目标对象，不强绑订单号 | 已采纳 |
| [ADR-0176](adr/0176-tool-arguments-gated-against-the-question.md) | 工具参数在网关侧对照原始提问做作用域放行校验 | 已采纳（设计定型） |
| [ADR-0177](adr/0177-site-query-has-one-implementation-three-entries.md) | 站点查询收口为一份后端实现、服务三处业务入口 | 已采纳（设计定型） |
| [ADR-0210](adr/0210-site-query-plan-precedes-tool-evidence.md) | 地点关键词先于工具取证确定，代理层复用统一站点查询实现 | 已采纳 |
| [ADR-0242](adr/0242-operator-run-metadata-readback.md) | Java 运行元数据按调用者当前本人权限读取，与业务结果解耦 | Accepted |
| [ADR-0249](adr/0249-run-log-detail-projection.md) | 运行日志投影扩展涵盖用户问题、订单号与执行步骤明细 | Accepted |
| [ADR-0251](adr/0251-run-log-tool-summaries-and-readable-trace.md) | 运行日志行级明细：工具入参/出参摘要投影及可人读 trace | Accepted |
| [ADR-0252](adr/0252-answer-review-observe-only.md) | 管家答复语义复核处于观察记录模式，不阻断诊断交付 | Accepted |

## 六、多环境开通、版本发布与容器分发（18 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0233](adr/0233-actions-delivery-flow.md) | GitHub Actions 交付流将检查、自动镜像同步与发布阶段职责分离 | Accepted |
| [ADR-0234](adr/0234-runner-queue-isolation.md) | 自托管 runner 执行槽与 PR 并发队列隔离 | Accepted |
| [ADR-0235](adr/0235-onboarding-dependencies-follow-authorities.md) | 客户环境开通依赖从已发布真源与系统声明动态收敛 | Accepted |
| [ADR-0256](adr/0256-platform-environment-registry.md) | 平台集中维护多业务环境目录与租户接入登记表 | Accepted |
| [ADR-0272](adr/0272-managed-gateway-install.md) | 客户网关最小接入模块支持独立受管安装与有界回滚恢复 | 已采纳 |
| [ADR-0274](adr/0274-bounded-shared-execution-ingress.md) | 共享执行节点入口仅暴露有界受限的诊断派发接口 | accepted |
| [ADR-0275](adr/0275-shared-environment-node-egress.md) | 每环境执行节点使用固定网关回调地址与共享模型出口 | accepted |
| [ADR-0304](adr/0304-preserve-installed-tenant-runtime.md) | 新增租户运行时快照按累积更新写入，不得覆盖同环境已有租户 | Accepted |
| [ADR-0308](adr/0308-cold-installation-before-preflight.md) | 声明含初装的真空环境由受管冷装先行执行，再开展现场预检 | 已采纳 |
| [ADR-0329](adr/0329-bounded-install-transfer-budget.md) | 跨区域大体积制品搬运共享统一传输预算与超限控制 | 已采纳 |
| [ADR-0333](adr/0333-explicit-execution-environment-adoption.md) | 旧执行环境向唯一运行目录一次性受控迁移与 CAS 恢复 | 已采纳 |
| [ADR-0335](adr/0335-environment-directory-overview-and-node-visibility.md) | 管理控制台「环境与租户」总览聚合视图与运行节点可见性口径 | 已采纳 |
| [ADR-0336](adr/0336-tenant-one-click-onboarding-orchestration.md) | 一键租户开通编排及同摘要开通计划幂等续签机制 | 已采纳 |
| [ADR-0347](adr/0347-multi-tenant-independent-publication-restore.md) | 同环境多租户发布后支持按非 LIFO 任意顺序独立停用与恢复 | 已采纳 |
| [ADR-0348](adr/0348-release-on-main-sha-dispatch.md) | `main` 代码集成与显式版本发布严格分离 | 已采纳 |
| [ADR-0358](adr/0358-zero-cli-self-service-environment-onboarding.md) | 面向交付运维的零命令行自助开通 V2 产品化总方案 | Accepted |
| [ADR-0361](adr/0361-day2-environment-release-upgrade-and-rollback.md) | 已上线客户环境 Day-2 零命令行版本升级与原子安全回滚 | 已采纳 |
| [ADR-0363](adr/0363-customer-gateway-oci-container-distribution.md) | 客户机 Gateway 通过阿里云 ACR 标准 OCI 容器镜像分发与无损切换 | 已采纳 |

## 七、仓库治理、测试分层、CI 与归档规则（15 份）

| ADR | 生效核心规则 | 状态备注 |
|---|---|---|
| [ADR-0002](adr/0002-trusted-pr-control-plane.md) | 受信任的 GitHub Actions PR 控制面与凭证隔离模型 | Accepted |
| [ADR-0011](adr/0011-adr-check-scope-and-self-application.md) | `adr.check.mjs` 的 diff 级结构/覆盖率门禁及自举应用边界 | 已采纳 |
| [ADR-0030](adr/0030-test-doubles-must-be-bitter-than-reality.md) | 测试替身必须比真实上游更严格（Bitter doubles），禁止乐观静默放行 | 已采纳 |
| [ADR-0032](adr/0032-spec-tree-as-intent-truth.md) | `.spec/` 规格树作为意图侧真值，门禁跑在 CI 而不阻塞本地 commit | 已采纳 |
| [ADR-0094](adr/0094-parallel-capability-is-a-declared-dependency.md) | pytest 并行能力必须锁定为项目显式 dev 依赖与环境属性 | Accepted |
| [ADR-0215](adr/0215-ci-test-tier-follows-the-diff.md) | CI 按变更路径分档运行（纯文档提交不触发全量测试）与下限保护 | 已采纳 |
| [ADR-0229](adr/0229-review-execution-owns-no-host-git.md) | 独立代码审查运行于隔离容器且不接触宿主 git 与敏感凭据 | Accepted |
| [ADR-0245](adr/0245-llmdoc-knowledge-surface.md) | `llmdoc/` 检索面与 `docs/` 手册/证据承载分工 | Accepted |
| [ADR-0269](adr/0269-bounded-doc-first.md) | 有界 Doc-first 准则：按权威来源增量更新，不重复记录进度与回执 | 已采纳 |
| [ADR-0345](adr/0345-offload-legacy-receipts-to-archive-repo.md) | 历史验收回执与开发进度迁入独立私有归档仓，主仓保留现役引用 | 已采纳 |
| [ADR-0355](adr/0355-tier-pytest-markers-and-ci-heavy-paths.md) | Pytest 慢速/重型标记分层与 CI 按改动路径按需触发规则 | Accepted |
| [ADR-0357](adr/0357-converge-p2-test-markers-ci-gating-and-hotspots.md) | P2 依赖核查收口：`deploy_tooling` CI 路径门禁、`nginx` 标记与热点收敛 | Accepted |
| [ADR-0364](adr/0364-automate-branch-lifecycle-and-mirror-cleanup.md) | 自动清理未合入关闭 PR 分支及公司 GitLab 每周周期镜像兜底 | Accepted |
| [ADR-0370](adr/0370-archive-superseded-and-tactical-adrs.md) | 本索引与 231 份历史/局部 ADR 归档迁出规则与编号水位线声明 | 已采纳 |
| [ADR-0371](adr/0371-automated-doc-lifecycle-and-compaction.md) | 文档自动减债闭环（Doc-GC）：写入控制、失鲜检测、定时 Autovacuum 与一键迁冷 | 已采纳 |

---

## 归档检索与编号规则

- **历史 ADR 水位线**：目前最大已分配编号为 **`0371`**。新增 ADR **必须从 `0372` 起顺延递增**，禁止复用已归档的旧编号（见 [决定记录约定](agents/architecture-decisions.md#writing-one) 与 [策略配置](doc-lifecycle.json)）。
- **检索已归档的历史 231 份 ADR**：
  - 所有已归档 ADR 均保留在私有仓库 [`kilbertert/AI-Ops-history-archive@4bed22cb`](https://github.com/kilbertert/AI-Ops-history-archive/tree/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr) 下的 `docs/adr/NNNN-*.md`；
  - 克隆归档仓库后可在本地通过 `sha256sum -c MANIFEST.sha256` 校验完整性，并通过 `ARCHIVE_LIST.json` 按源修订、标题或编号检索。
