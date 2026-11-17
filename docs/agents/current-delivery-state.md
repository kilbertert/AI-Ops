# 当前交付状态索引

维护：对应领域负责人；基线为各行引用的回执，不是实时监控。本页不累积发布历史。
变更替换对应领域的结论与证据；未复验范围保留，不能由其他组件的新版本推导通过。
普通任务进度在原Issue；日志、账号和主机快照保持私有。完整维护规则见[文档导航](../README.md#变更前与交付时)。

## 环境目录与开通试点

适用：共享管理入口、目标环境接入准备；各行证据有独立日期，较新的本地验证不替代其他组件生产复验。

| 能力/组件 | 已合并 | 已部署 | 已启用 | 已验收与缺口 | 证据 |
|---|---|---|---|---|---|
| 目录草稿/Gateway、管理桥 | #1548 `9c21d855` | 基础制品已配套；Gateway后续至#1551的1.14.1 | 管理目录准备已配置、0个生效环境；保存不改业务准入 | 隔离持久化/权限/CAS通过；真实租户开通未验 | [基础验证](../validation/2026-11-16-environment-directory-drafts.md)、[配套部署](../validation/2026-11-16-z-environment-tab-delivery.md)、[准备配置](../validation/2026-11-16-z-console-session-live.md) |
| 独立环境页/Dify Web | #1549 `d136bdb2` | 完整Web已配套；当时Gateway 1.14.0，后续至1.14.1 | 路由存在；不代表目录或租户已激活 | 独立浏览器交互和匿名拒绝通过；正常账号真实管理、预检/安装/激活及三功能业务验收未完成 | [环境页回执](../validation/2026-11-16-z-environment-tab-delivery.md) |
| 目标登录/共享服务临时网络 | #1544 `b1f8156a` | 备用入口按回执配套 | 仅获准来源，原入口保留 | 登录身份/授权单次读与SSE通过；正式网络稳定性、双向工具往返和业务容量未闭环；临时入口复查见#1546 | [网络回执](../validation/2026-11-16-z-shared-dify-recovery-live.md) |
| 活动登录/管理目录准备 | #1551 `9089bea9` | Gateway 1.14.1与登录代理完整包已配套 | 管理目录revision1、0个生效环境、1个显式管理员；运行准入未切换 | 正常登录/管理读取、退出撤销及重登恢复通过；日志正向受应用权限阻塞，发布权限、页面保存及非管理员现场仍未验 | [部署回执](../validation/2026-11-16-z-console-session-live.md)、[共享会话复验](../validation/2026-11-17-deployable-increments.md) |
| 最小接入模块候选预演 | #1554 `864896f0` | 第二候选环境已运行固定3.12离线制品，预演后清理 | 未切换正式入口，未激活租户 | 双实例启动/版本/匿名拒绝/健康请求/清理通过，单实例RSS约76MiB；既有worker重启异常、正式网络/业务负载/共享节点及升级恢复仍未闭环 | [目标预演回执](../validation/2026-11-16-gateway-candidate-live.md) |
| 正式HTTPS工具扩展 | #1556实现与验证，合并状态以PR为准 | 尚未安装目标入口 | 未切流量、未启用租户 | 本地TLS/来源/路径/方法与独立复验通过；目标域名TLS可达但回调404；宿主日志前置、入口安装授权、真实工具往返仍待闭环 | [入口验证](../validation/2026-11-17-environment-tool-ingress.md) |
| 公司来源门 | #1532 `08d203f1` | 工件已交付 | 未配置、未启用 | 不证明双环境签发身份隔离；现规划不再以公司改码为前提 | [来源门回执](../validation/2026-11-14-z-environment-delivery-receipt.md) |
| 旧第二环境试验 | 历史实现保留 | 旧试验运行资产已撤回 | 不恢复旧完整栈或专属业务隧道 | 公司当前健康未重新确认；历史部分成功不等于完整业务验收 | [撤回回执](../validation/2026-11-14-z95-withdrawal-and-gateway-plan.md) |

获批行为唯一正文在[环境目录Feature](../acceptance/environment-directory.feature)，
[QA](../acceptance/environment-directory-qa-plan.md)给出场景步骤，[手册](environment-registry.md)讲操作。
未来能力与依赖见[规划](../architecture/2026-11-16-manual-environment-onboarding.md)和主任务#1535/P0～P5原生子任务。
页面已部署不关闭P0/P1/P2剩余条件；P3安装、P4开通和P5双环境业务验收仍待完成。
取证时按领域、组件、环境与适用主体选择回执，不选择全目录字典序最大文件。

## 客服、管家与共享能力

下表只保持已有证据范围，不因环境目录制品更新而宣称所有功能已重新验收。

| 领域 | 最后有效结论/适用边界 | 证据与操作入口 |
|---|---|---|
| 客服正式问答与选库 | 原Dify/客户端API的选库、提示词和新发布版本已有回执；App/H5屏幕与知识准确性未验收 | [选库恢复](../validation/2026-11-14-consumer-publication-selection-live.md)、[手册](consumer-wf01/knowledge-picker-deploy.md) |
| 自然回复与金额事实 | 11月12日复验保留主客户端V35/revision43；有限原Dify/H5场景通过，业务准确性与任意计算诱导仍未证明 | [最终回执](../validation/2026-11-12-response-policy-final-acceptance.md)、[事实规则](consumer-wf01/order-composition-qa.md) |
| 管家问答/证据交付 | 正常答复与仅证据交付均有原入口记录；语义复核为观察，授权/完整性仍是拒绝闸；复杂问句耗时和准确性仍有边界 | [复核调整](../validation/2026-11-12-operator-review-observe-deploy.md)、[证据交付](../validation/2026-11-12-operator-evidence_02-live.md) |
| 管家运行日志 | 事件明细与复核行渲染有制品及页面证据；生产非allow/refused缺自然样本 | [渲染回执](../validation/2026-11-13-run-log-review-render-deploy.md)、[日志手册](operator-cpms/run-logs.md) |
| 设备离线/统计 | 指定设备的区间计算与独立记录一致，原Dify/H5追问有限通过；无工程师确认的物理故障结论、无全量准确率 | [离线回执](../validation/2026-11-10-device-offline-live-acceptance.md)、[方法](operator-cpms/device-offline.md) |
| 站点/费用/发布恢复 | 指定原入口场景有证据；独立服务费折扣、优惠资格、结算状态、关联占枪费、非零退款及完整双租户矩阵仍有缺口 | [反馈验收](../validation/2026-11-04-tester-feedback-production.md)、[站点验收](../validation/2026-11-04-site-query-real-path-deployed.md) |
| 客户AI开通与知识 | 既有客户失败续跑、凭据持久化与隔离有证据；不是新环境自动开通或任意租户资料质量验收 | [开通回执](../validation/2026-11-06-onboarding-deployed-receipt.md)、[知识验收](../validation/2026-10-29-dify-knowledge-live.md)、[手册](operator-cpms/self-service.md) |
| 手机/页面入口 | 原APK自由单轮有证据；#1353连续会话与#1422Markdown呈现仍待新版包。管家首页入口已撤回，聊天路由保留 | [客服回执](../validation/2026-11-05-consumer-agent-production-closeout.md)、[入口撤回](../validation/2026-11-03-operator-h5-entry-withdrawal.md) |
| 退役与历史数据 | 旧Python运行时、诊断/设备CLI与宣传路径已退役；客户案例数据行停用，前端硬编码仍有偏差；不恢复旧入口 | [运行时回执](../validation/2026-10-29-runtime-retirement-live.md)、[案例回执](../validation/2026-11-14-case-exploration-retired.md)、[退役边界](../runtime-retirement.md) |
| 持久化与交付 | schema阶段2及运行日志后继迁移有各自版本证据；不把旧schema数字当当前生产值，不把CI/镜像同步当运行验收 | [schema回执](../validation/2026-11-11-gateway-schema-live-receipt.md)、[日志迁移](../validation/2026-11-12-runlog-detail-v2-deploy.md)、[Actions手册](actions-delivery.md) |

人工语义准确率、长期稳定性、生产在途发布竞争、原生APK/IPA和全面多语言质量仍未整体验收。
产品样例、双租户权限/发布矩阵和费用语义继续在对应Issue中跟踪，不以关闭票据代替验收。

## 复核与历史

- 回执是时点证据；若目标版本、环境、权限或依赖已变，重新取证并更新相应行，保留此前失败。
- [系统架构](../system-architecture.md)、[测试交接](../testing-handoff.md)、[阶段台账](../reviews/2026-11-05-architecture-stage-closeout.md)分别回答职责、验收交接和旧任务处置。
- 历史过程从[验收目录](../validation/)、[旧进度](../开发进度/)按任务查；旧完整索引可从Git修订`734f0201`读取。
  本次不搬运或删除历史回执，`docs/validation.md`和`docs/开发进度.md`仍为只读归档。
