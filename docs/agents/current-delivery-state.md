# 当前交付状态索引

维护：对应领域负责人；基线为各行引用的回执，不是实时监控。本页不累积发布历史。
变更替换对应领域的结论与证据；未复验范围保留，不能由其他组件的新版本推导通过。
普通任务进度在原Issue；日志、账号和主机快照保持私有。完整维护规则见[文档导航](../README.md#变更前与交付时)。

## 环境目录与开通试点

适用：共享管理入口、目标环境接入准备；各行证据有独立日期，较新的本地验证不替代其他组件生产复验。

| 能力/组件 | 已合并 | 已部署 | 已启用 | 已验收与缺口 | 证据 |
|---|---|---|---|---|---|
| 目录草稿/Gateway、管理桥 | #1548基础；#1580投影；#1582 `3fdf6373`安装接续 | 控制Gateway/管理桥1.24.0配套 | 中央生效目录revision1、0个环境；第二环境草稿revision1、两个租户均未启用 | 隔离持久化/权限/CAS、现场CLI保存回读及正常管理读取已有证据；目标成套安装另验；原页面保存、激活与真实开通未验 | [基础验证](../validation/2026-11-16-environment-directory-drafts.md)、[原管理页回执](../validation/2026-11-18-environment-projection-live.md)、[当前控制部署](../validation/2026-11-18-candidate-knowledge-live.md)、[目标安装](../validation/2026-11-18-paired-config-install-live.md) |
| 独立环境页/Dify Web | #1549基础；#1580投影；#1587 `27e78825`任务页；#1592工作区入口；#1594知识入口 | 1.24.0同修订完整Web已配套 | 路由与预检计划可用；目录和租户未激活 | 正常公司登录/页面读取、预检重复/回读、后台自动处理及页面重载/退出拒绝通过；原页面保存、远程安装/激活及三功能未验 | [当前页面回执](../validation/2026-11-18-candidate-knowledge-live.md)、[任务基础事件](../validation/2026-11-18-central-preflight-task-live.md)、[投影事件](../validation/2026-11-18-environment-projection-live.md)、[基础环境页](../validation/2026-11-16-z-environment-tab-delivery.md) |
| 配置/制品预检与候选投影 | #1575基础；#1578归档；#1580投影；#1582 `3fdf6373`私有目录路径 | 控制Gateway/Web/桥1.24.0配套 | 已存草稿生成持久计划；受控引用已配置，文件/投影分项通过，现场业务待验且executable=false | 隔离投影/凭据/导出及目标成套安装有证据，生产文件预检与自动现场任务已验；网页安装执行、来源/容量/网络/业务仍待验 | [当前生产预检](../validation/2026-11-18-candidate-knowledge-live.md)、[投影回执](../validation/2026-11-18-environment-projection-live.md)、[安装接续](../validation/2026-11-18-paired-config-install-live.md)、[归档回执](../validation/2026-11-18-environment-archive-preflight-live.md) |
| 目标登录/共享服务临时网络 | #1544 `b1f8156a` | 备用入口按回执配套 | 仅获准来源，原入口保留 | 登录身份/授权单次读与SSE通过；正式网络稳定性、双向工具往返和业务容量未闭环；11月18日原路径仍失败，备用认证/流式复查通过且有实际使用；续期至11月20日14:04北京时间，#1546保持open | [网络回执](../validation/2026-11-16-z-shared-dify-recovery-live.md) |
| 活动登录/管理目录准备 | #1551 `9089bea9` | Gateway 1.14.1与登录代理完整包已配套 | 管理目录revision1、0个生效环境、1个显式管理员；运行准入未切换 | 正常登录/管理读取、退出撤销及重登恢复通过；日志正向受应用权限阻塞，发布权限、页面保存及非管理员现场仍未验 | [部署回执](../validation/2026-11-16-z-console-session-live.md)、[共享会话复验](../validation/2026-11-17-deployable-increments.md) |
| 最小接入模块与成套配置 | #1558安装；#1561就绪；#1562单配置；#1582 `3fdf6373`成套恢复 | 第二环境独立实例1.19.0+3fdf6373205e，仅回环；目录本地revision3 | 原MySQL/Redis及公司时序HTTPS连接保留；环境/两个租户停用，未接模型应用 | CI5290/10、独立29项及现场两文件安装/重复/目录写后硬退出恢复通过；原Gateway及容器ID保持，既有worker重启另记录；冷安装全矩阵、业务和长期容量待验 | [当前安装回执](../validation/2026-11-18-paired-config-install-live.md)、[原业务连接](../validation/2026-11-17-business-connection-live.md)、[原安装](../validation/2026-11-17-gateway-install-live.md) |
| 已安装实例现场预检 | #1584 `277cb03a` | 控制Gateway1.20.0已CD；同合并安装器已在第二环境执行，目标应用仍1.19.0 | 只读本地回执，不连接中央执行；环境/租户停用 | CI5298/10、独立8项复验与现场三次同编号通过；8项本地passed/3项身份网络业务pending，原文件/数据库/PID保持；中央整合、业务及容量未验 | [本次现场回执](../validation/2026-11-18-installed-instance-preflight-live.md)、[手册](../../ops/environments/gateway/install.md#已安装实例现场预检) |
| 中央现场预检持久任务 | #1587/#1588基础；#1590 `ae6e21c8`自动领取 | 控制/Web/桥1.24.0配套；目标应用仍1.19.0 | 生产refs/key与用户timer已配套，只读预检自动处理；环境/租户停用 | CI5331/10、独立取消/队列反例、正常页面提交到实际后台/124回执通过；重复/重载同任务、退出拒绝、原配置/PID保持；候选知识另验；真实业务、应用与激活未验 | [当前回执](../validation/2026-11-18-candidate-knowledge-live.md)、[自动任务事件](../validation/2026-11-18-automatic-preflight-live.md)、[基础事件](../validation/2026-11-18-central-preflight-task-live.md)、[手册](environment-registry.md#自动执行端配套) |
| 候选工作区准备 | #1592 `5cc1d8b3` | 控制Gateway、管理桥及完整Web1.24.0配套；登录代理1.15.0兼容保持 | 一个已登记测试租户候选工作区；原目录与目标租户停用 | CI5345/10、独立8＋4组恢复反例通过；正常页面→后台→实际工作区、重复/刷新、唯一归属、退出/旧会话拒绝通过；候选知识见下一行；应用/业务与激活未验 | [工作区事件](../validation/2026-11-18-candidate-workspace-live.md)、[当前配套部署](../validation/2026-11-18-candidate-knowledge-live.md)、[手册](environment-registry.md#候选工作区准备) |
| 候选知识资源准备 | #1594 `3a6ef7e0` | 控制Gateway、管理桥及完整Web1.24.0配套；登录代理1.15.0兼容保持 | 一个测试租户独立知识空间及空库；环境/租户仍停用 | CI5352/10；独立7＋6＋2组输入通过；正常页面→后台→真实空库、当前模型配置/凭据验证、重复/刷新和退出拒绝通过；应用、知识连接、检索内容与业务/激活未验 | [本次回执](../validation/2026-11-18-candidate-knowledge-live.md)、[手册](environment-registry.md#候选知识资源准备) |
| 正式HTTPS工具扩展 | #1556 `981b6e09`，固定制品取#1562 | 已安装既有API域名工具扩展，仅共享宿主来源到新回环实例 | 工具传输路径已接通；原公司入口/旧Gateway保留，租户未激活 | 两共享宿主匿名/错凭据401、路径/方法/HTTP及伪造来源拒绝通过；合法run工具往返与业务问答待验 | [业务连接回执](../validation/2026-11-17-business-connection-live.md)、[入口合同验证](../validation/2026-11-17-environment-tool-ingress.md) |
| 共享执行HTTPS派发 | #1564 `01f8af66`，固定上游/实际来源/单一路径 | 已安装既有HTTPS扩展，现役平台保持 | 目标来源可到平台鉴权；新节点见下一行，租户未激活 | 目标六项、另一来源伪造头拒绝及真实应用鉴权层通过；合法run仍待验 | [现场回执](../validation/2026-11-17-shared-execution-ingress-live.md)、[开发验证](../validation/2026-11-17-shared-execution-ingress.md)、[手册](../../ops/environments/gateway/execution-ingress.md) |
| 第二环境独立共享节点 | #1566 `d9072f49`、#1567 `f47d1f42` | 独立两容器与systemd已部署，固定节点jar；原共享四容器保持 | 新执行组一个在线节点；目标应用与租户未开通 | 节点Java双TLS到目标拒绝、身份/网络/原资源保持通过；真实应用目标、能力授权、合法run和并发容量待验 | [现场回执](../validation/2026-11-17-shared-environment-node-live.md)、[手册](../../deploy/node-runtime/environment.md) |
| 客服连接唯一目录 | #1569 `c4836cfb` | 控制Gateway 1.15.0已标准CD部署；目标独立Gateway未随之升级 | 运行目录模式不写旧连接副本；现役仍管理准备模式 | 隔离58项、独立44+3及CI5232/10通过；原路由/目录/六容器保持，目录投影与真实双环境业务仍未验 | [开发回执](../validation/2026-11-17-registry-consumer-connections.md)、[原部署回执](../validation/2026-11-17-registry-consumer-live.md)、[当前部署](../validation/2026-11-17-console-environment-deployed.md) |
| 共享登录选择环境 | #1572 `bfd14ad9` | 41 Gateway及36完整登录代理/页面/桥1.15.0已配套 | 多环境关闭；单环境41保持，生效目录0 | CI5240/10、独立反例修复/重放通过；正常公司登录→管理页读取→原生退出→旧会话拒绝通过；124及双环境业务未验 | [部署复验](../validation/2026-11-17-console-environment-deployed.md)、[开发验证](../validation/2026-11-17-console-environment-sessions.md) |
| 公司来源门 | #1532 `08d203f1` | 工件已交付 | 未配置、未启用 | 不证明双环境签发身份隔离；现规划不再以公司改码为前提 | [来源门回执](../validation/2026-11-14-z-environment-delivery-receipt.md) |
| 旧第二环境试验 | 历史实现保留 | 旧试验运行资产已撤回 | 不恢复旧完整栈或专属业务隧道 | 公司当前健康未重新确认；历史部分成功不等于完整业务验收 | [撤回回执](../validation/2026-11-14-z95-withdrawal-and-gateway-plan.md) |

获批行为唯一正文在[环境目录Feature](../acceptance/environment-directory.feature)，
[QA](../acceptance/environment-directory-qa-plan.md)给出场景步骤，[手册](environment-registry.md)讲操作。
未来能力与依赖见[规划](../architecture/2026-11-16-manual-environment-onboarding.md)和主任务#1535/P0～P5原生子任务。
页面已部署不关闭P0/P1/P2剩余条件；P3基础安装、就绪修复和配置事务已有证据，完整业务配置与恢复矩阵仍待补齐；
P4文件预检、冻结计划、停用候选投影/导出及指定目标成套安装恢复及本地现场预检已有证据；中央只读任务持久去重/失联核对及生产自动处理已验；候选工作区准备及真实回读已验，未知恢复在隔离反例验证；候选知识空间与空库及正常页面已验；应用/知识连接准备、完整身份/网络/业务预检、安装任务与激活和P5双环境业务验收仍待完成。
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
