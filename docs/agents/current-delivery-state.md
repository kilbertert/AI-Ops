# 当前交付状态索引

维护：对应领域负责人；基线为各行引用的回执，不是实时监控。本页不累积发布历史。
变更替换对应领域的结论与证据；未复验范围保留，不能由其他组件的新版本推导通过。
普通任务进度在原Issue；日志、账号和主机快照保持私有。完整维护规则见[文档导航](../README.md#变更前与交付时)。

## 环境目录与开通试点

适用：共享管理入口、目标环境接入准备；各行证据有独立日期，较新的本地验证不替代其他组件生产复验。

| 能力/组件 | 已合并 | 已部署 | 已启用 | 已验收与缺口 | 证据 |
|---|---|---|---|---|---|
| 管理页草稿保存与权限 | 现役1.39代码未改；本轮验收归档 | 41控制/36桥/Web仍1.39，124未升级 | 真实124草稿显示名V9→10→11已恢复，生效目录R1不变 | 隔离Chromium→真桥/API/SQLite七场景通过：两环境同号新增、编辑、生效隔离、冲突、撤权和普通租户拒绝；生产正常登录保存/刷新/CAS恢复/退出通过；真实双环境生效管理与P5仍未验 | [本轮浏览器回执](../validation/2026-11-22-environment-draft-browser-api.md) |
| 目录草稿/Gateway、管理桥 | #1548基础；#1580投影；#1582 `3fdf6373`安装接续 | 控制Gateway/桥1.33.0配套 | 中央生效目录revision1、0个环境；第二环境草稿V7，实际目标准入仍关闭 | 隔离持久化/权限/CAS、现场CLI保存回读及正常管理读取已有证据；目标成套安装另验；原页面保存、激活与真实开通未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[基础验证](../validation/2026-11-16-environment-directory-drafts.md)、[目标安装](../validation/2026-11-18-paired-config-install-live.md) |
| 独立环境页/Dify Web | #1549基础；#1580投影；#1587 `27e78825`任务页；#1592工作区入口；#1594知识入口；#1597连接入口；#1600管家准备入口；#1602消费者准备入口；#1631串联入口 | 1.33.0控制Gateway/桥/Web配套 | 路由与预检计划可用；目录和租户未激活 | 正常公司登录、一次提交→后台六步、执行中重载/重复同编号、退出拒绝通过；页面保存、远程安装/激活及三功能未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[原任务基础](../validation/2026-11-18-central-preflight-task-live.md) |
| 配置/制品预检与候选投影 | #1575基础；#1578归档；#1580投影；#1582 `3fdf6373`私有目录路径 | 控制Gateway/Web/桥1.33.0配套 | 已存草稿生成持久计划；受控引用已配置，文件/投影分项通过，现场业务待验且executable=false | 隔离投影/凭据/导出及目标成套安装有证据，生产文件预检与自动现场任务已验；网页安装执行、来源/容量/网络/业务仍待验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[投影回执](../validation/2026-11-18-environment-projection-live.md)、[安装接续](../validation/2026-11-18-paired-config-install-live.md) |
| 目标登录/共享服务HTTPS入口 | #1544 `b1f8156a`；正式用途见ADR0306 | 既有受限入口配置不变，目标正式客服尚未配URL/启用 | 用户11月22日批准该同域受限入口作为124正式服务API入口；原入口保留 | 既有认证/SSE时点证据保留；本轮默认TLS主机名/证书验证后匿名401；原443仍不稳定，完整双向业务/容量未验；转正不触发目标重启或准入 | [ADR0306](../adr/0306-formal-scoped-dify-service-ingress.md)、[网络回执](../validation/2026-11-16-z-shared-dify-recovery-live.md) |
| 活动登录/管理目录准备 | #1551 `9089bea9` | Gateway 1.14.1与登录代理完整包已配套 | 管理目录revision1、0个生效环境、1个显式管理员；运行准入未切换 | 正常登录/管理读取、退出撤销及重登恢复通过；日志正向受应用权限阻塞，发布权限、页面保存及非管理员现场仍未验 | [部署回执](../validation/2026-11-16-z-console-session-live.md)、[共享会话复验](../validation/2026-11-17-deployable-increments.md) |
| 最小接入模块与成套配置 | #1558安装；#1561就绪；#1562单配置；#1582 `3fdf6373`成套恢复 | 第二环境独立实例1.19.0+3fdf6373205e，仅回环；目录本地revision3 | 原MySQL/Redis及公司时序HTTPS连接保留；环境/两个租户停用，未接模型应用 | CI5290/10、独立29项及现场两文件安装/重复/目录写后硬退出恢复通过；原Gateway及容器ID保持，既有worker重启另记录；冷安装全矩阵、业务和长期容量待验 | [当前安装回执](../validation/2026-11-18-paired-config-install-live.md)、[原业务连接](../validation/2026-11-17-business-connection-live.md)、[原安装](../validation/2026-11-17-gateway-install-live.md) |
| 已安装实例现场预检 | #1584 `277cb03a` | 控制Gateway1.20.0已CD；同合并安装器已在第二环境执行，目标应用仍1.19.0 | 只读本地回执，不连接中央执行；环境/租户停用 | CI5298/10、独立8项复验与现场三次同编号通过；8项本地passed/3项身份网络业务pending，原文件/数据库/PID保持；中央整合、业务及容量未验 | [本次现场回执](../validation/2026-11-18-installed-instance-preflight-live.md)、[手册](../../ops/environments/gateway/install.md#已安装实例现场预检) |
| 中央现场预检持久任务 | #1587/#1588基础；#1590 `ae6e21c8`自动领取 | 控制Gateway/Web/桥1.33.0；目标Gateway1.32.2 | 生产refs/key与用户timer已配套，只读预检自动处理；环境/租户停用 | CI5331/10、独立取消/队列反例、正常页面提交到实际后台/124回执通过；重复/重载同任务、退出拒绝、原配置/PID保持；候选知识另验；真实业务、应用与激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[自动任务事件](../validation/2026-11-18-automatic-preflight-live.md)、[手册](environment-registry.md#自动执行端配套) |
| 候选工作区准备 | #1592 `5cc1d8b3` | 控制Gateway/桥/Web1.33.0，目标1.32.2；Dify原API保持 | 一个已登记测试租户候选工作区；原目录与目标租户停用 | CI5345/10、独立8＋4组恢复反例通过；正常页面→后台→实际工作区、重复/刷新、唯一归属、退出/旧会话拒绝通过；候选知识见下一行；应用/业务与激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[工作区事件](../validation/2026-11-18-candidate-workspace-live.md)、[当前配套部署](../validation/2026-11-18-candidate-consumer-live.md)、[手册](environment-registry.md#候选工作区准备) |
| 候选知识资源准备 | #1594 `3a6ef7e0` | 控制Gateway/桥/Web1.33.0，目标1.32.2；Dify原API保持 | 一个测试租户独立知识空间及空库；环境/租户仍停用 | CI5352/10；独立7＋6＋2组输入通过；正常页面→后台→真实空库、当前模型配置/凭据验证、重复/刷新和退出拒绝通过；知识连接见下一行；应用、检索内容与业务/激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[资源事件](../validation/2026-11-18-candidate-knowledge-live.md)、[当前配套](../validation/2026-11-18-candidate-consumer-live.md)、[手册](environment-registry.md#候选知识资源准备) |
| 候选知识连接准备 | #1597 `c367d6d9`；#1598 `9514e540`40字符协议修复 | 控制Gateway/桥/Web1.33.0，目标1.32.2；Dify原API保持 | 一个测试租户受限单库API；环境/租户仍停用 | CI5365/10、本地5366/9及独立schema/恢复/撤权反例通过；正常页面→后台→连接、重复/刷新、单库清单/错签名/错来源拒绝、退出拒绝通过；空库检索仅接线，管家准备见下一行；内容/业务与激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[连接事件](../validation/2026-11-18-candidate-connection-live.md)、[当前配套](../validation/2026-11-18-candidate-consumer-live.md)、[手册](environment-registry.md#候选知识连接准备) |
| 候选管家应用准备 | #1600 `8768f82f` | 控制Gateway/桥/Web1.33.0，目标1.32.2；Dify原API保持 | 一个测试租户候选agent-chat，API/site停用、keys=0，环境/租户停用 | CI5369/10、本地5370/9；独立取消/漂移/失联/POST边界反例通过；正常页→后台→应用、重复/刷新、模型/SOP/11工具回读及退出拒绝通过；消费者见下一行；执行目标/真实run/业务与激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[本次回执](../validation/2026-11-18-candidate-operator-live.md)、[手册](environment-registry.md#候选管家应用准备) |
| 候选消费者应用准备 | #1602 `904472df` | 控制Gateway/桥/Web1.33.0，目标1.32.2；Dify原API保持 | 原测试租户及124/LTA-CSMS各有一个候选advanced-chat，均为入口停用；环境/租户未激活 | #1602的CI5375/10、Chromium8及独立取消/漂移/失联/分离路由库/所选版本反例通过；两次正常页面流程均完成候选准备与退出；124现场身份/容量/正式网络/业务pending，真实模型、知识内容/取数与激活未验 | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[124 LTA-CSMS事件](../validation/2026-11-19-lta-candidate-consumer-live.md)、[候选准备实现回执](../validation/2026-11-18-candidate-consumer-live.md)、[手册](environment-registry.md#计划预检与读取（P4第一片）) |
| 一次候选准备持久任务 | #1631 `d211d184` | 控制Gateway/桥/Web1.33.0，原timer配套；124仍1.32.2 | 候选资源复用，环境/租户与正式入口仍关闭 | CI5513/10、独立并发/取消/撤权3项及修复重放通过；正常页一次提交→后台六步→重载/重复同父编号→退出拒绝通过；安装/激活未执行，业务unverified | [串联配套与页面验收](../validation/2026-11-20-candidate-preparation-live.md)、[D36手册](environment-registry.md#一次准备候选资源d36) |
| 停用候选安装持久任务 | #1633 `b7dde17c`；#1635 `c05b3699`引用；#1636 `e790361f`暂存 | 控制Gateway1.34.1/桥1.34.0，目标实例1.34.0+c05b3699 | LTA-CSMS停用候选配置/运行工件已成套安装；注册/业务/激活均未执行，正式入口关闭 | 首任务stage因`/etc`只读沙箱失败并修复暂存路径；正常页提交→专用timer→远端意图+原安装器事务→本地观察通过；控制库持久、同计划重复同父、整页重载回读与退出拒绝通过 | [本次安装回执](../validation/2026-11-22-candidate-install-task-live.md)、[D37手册](environment-registry.md#停用候选安装任务d37) |
| 成套冷装与跨修订恢复 | 应用/安装器#1647 `81d90e02`；声明#1649 `0823ee6e` | 独立空实例完成1.38冷装及1.36.1→1.38中断恢复，已停用归档；现役124保持1.36.1 | 无业务凭据/公网入口，合成租户停用 | 真实systemd隔离/权限、重复PID保持、冷失败保留、切换及恢复再中断后成功、目录单调/状态保持通过；旧版两次前置OSError未确因，本地另复现TIME_WAIT预检误拒待修；不等业务/容量验收 | [现场回执](../validation/2026-11-22-isolated-install-recovery-live.md) |
| 正式发布、最后准入与恢复 | #1651 `c9d8f03c`，1.39.0 | 41标准CD、36桥/Web与原worker已配套；124仍1.36.1 | 未签正式grant、未启用环境/租户 | 最终CI5671/10；独立H1/H2复验、PR取消恢复修复通过；正常公司页三项缺资料回读、无发布入口、刷新/退出/旧会话拒绝通过；真实业务/容量/目标发布准入恢复未验 | [控制面现场](../validation/2026-11-22-environment-publication-control-live.md)、[开发验证](../validation/2026-11-22-environment-publication-development.md)、[D43手册](environment-registry.md#正式发布最后准入与恢复d43) |
| 多租户运行共存与安装恢复 | #1647 `81d90e02`，1.38.0 | 41标准CD已部署；124仍1.36.1，未应用组合运行包 | 保留旧准入/目标，新候选仍停用；不发布或授权知识 | CI5634/10、独立21条原安装/恢复/真实TCP问答路径通过；安装验证为真实文件/SQLite配主机命令替身，真实systemd维护及生产多租户业务未验；runtime/2未完成事务须同版兼容安装器恢复 | [PR与检查](https://github.com/kilbertert/AI-Ops/pull/1647)、[CD](https://github.com/kilbertert/AI-Ops/actions/runs/44973915014)、[ADR0304](../adr/0304-preserve-installed-tenant-runtime.md) |
| 跨环境正式知识受限读依赖 | #1645 `3253e6da`，1.37.0 | 41标准CD已部署；124未升级/未配新能力 | 未签发生产委派或grant，不启用租户 | CI5624/10、原客服HTTP/媒体及跨env/app/library/撤权重授权独立通过；非零Range/旧opaque key/大媒体截断均修复复验；真实共享服务与124正式路径仍未验 | [D41手册](environment-registry.md#正式受限知识读能力d41)、[ADR0302](../adr/0302-scoped-knowledge-delegation.md) |
| 已登记租户三业务技术验证 | #1643 `a485af9c`；#1644 `9b5d3d01`配置引用修复 | 41/124已1.36.1，桥/Web1.36配套；D41后继控制版本另记 | 空案例全部三能力SAMPLE_REQUIRED，未模型运行、环境/租户/正式路由仍关闭 | 全量CI5594/10及修复5606/10、独立运行授权/取消/配置CAS反例通过；正常页升级登记、缺样例阻塞、重复同任务/重载/退出补验通过；此前36→41 HTTPS unavailable/502失败保留，真实业务正向与长期网络未验 | [本事件回执](../validation/2026-11-22-installed-business-validation-live.md)、[D40手册](environment-registry.md#已安装租户业务验证d40) |
| 已安装候选运行登记续接 | #1641 `7f2e57c2`，1.35.0 | 控制41、124均1.35.0+7f2e57c2；管理桥/Web与worker已配套 | LTA工作区/双端运行资源已登记；环境/租户与正式路由仍关闭 | CI5577/10、独立取消与撤权恢复反例修复复验通过；正常页准备→升级→登记、重复/重载/退出拒绝通过；固定只读观察unchanged且登记不重启，业务未验 | [本次回执](../validation/2026-11-22-runtime-registration-task-live.md)、[D39手册](environment-registry.md#已安装候选运行登记d39) |
| 受管主机身份验收 | #1638 `5751838b`；#1639 `b8f1b73a`桥放行补漏 | 控制Gateway与桥`b8f1b73a`；worker沿既有site-preflight timer | 仅证明任务落在已登记主机，不代替正式网络、容量与业务验收；环境/租户停用 | 首验暴露桥白名单未放verify_task（请求到不了41）已修复；正常页提交→timer领取→dev-host指纹断言后对124只读采集，回执scope=managed-host-verification-only、executable=false、三字段齐全（hostname/machine-id摘要/服务身份）；同计划重复同任务、刷新回读与退出拒绝通过 | [本次验收回执](../validation/2026-11-22-host-verify-live.md)、[D38手册](environment-registry.md#受管主机身份验收d38) |
| 共享Dify流式并发转发 | #1627 `76b459dc` | 共享Dify原自有转发器已从合并固定包更新；Gateway保持1.32.2 | 原回环/鉴权与业务入口保持，第二环境仍停用 | CI5502/10及独立流式互等、32槽/异常释放与观察失败拒绝通过；真实41长连接期间Console200/70ms，单位/容器身份保持；完整模型工具业务后续单验 | [并发恢复事件](../validation/2026-11-20-dify-forward-concurrency-live.md)、[转发器手册](../../ops/dify-inner-api/README.md) |
| 真空机一键冷开通（含初装） | #1661 `41b09d5b`已合并，ADR0308，版本1.40.0 | 控制41/管理桥36/Web1.40.0已配套；95真实初装`1.40.0+41b09d5b`（127.0.0.1:8788，aiops身份），旧/opt/aiops-95不恢复 | 环境/租户`enabled:false`、正式路由0；页面「空机初装并准备」显式确认入口已真实走通 | 本地替身边界证据见开发验证；真实页面链：草稿同属判定→冻结计划→精确客服模板→显式确认→同源重复同父号→父observed且七步全observed（site-install复跑`already_installed`、preflight/workspace/knowledge/connect/operator/consumer）→重载回读→退出旧会话拒绝；首轮`SETUP_KNOWLEDGE_FAILED`经知识侧设计`rotate`接口收敛后重放通过；激活渲染/正式业务未验 | [现场回执](../validation/2026-11-22-pinggao-cold-install-live.md)、[开发验证](../validation/2026-11-22-environment-cold-onboarding-development.md)、[ADR0308](../adr/0308-cold-installation-before-preflight.md)、[手册](environment-registry.md#空机初装包含初装adr-0308) |
| 停用候选知识工具全链 | Gateway #1625 `b81141b1`；共享转发器#1627 `76b459dc` | 控制41/124 Gateway1.32.2，共享转发修复已部署 | LTA-CSMS候选库含1份获准验收资料/1分段，源应用及准入停用，正式路由保持 | 正常管理/C端登录→新鲜候选→一次knowledge found→原资料/分段/结果reference精确相符→completed与回收404/撤权403通过；仅合成资料的真实链路，本人订单/费用与P5客户端未验 | [知识全链事件](../validation/2026-11-20-candidate-consumer-knowledge-live.md)、[D35手册](environment-registry.md#停用客服候选的受限试运行（D35）) |
| 停用客服候选试运行 | #1622 `888df22a`；#1623 `22856c04`；#1625 `b81141b1` | 控制41及124自有Gateway1.32.2已部署；桥/Web保持1.27.0 | 新鲜LTA-CSMS候选复用且入口停用，环境与正式路由未激活 | 最新CI5496/10及独立撤权、Console503/缺证拒绝通过；1.32.0正常C端身份、HTTPS核权、模型succeeded、回读/清理及取消403已有时点证据；该轮Console核权超时、calls为空且本人无订单；候选知识链后继见上一行，真实订单仍未验 | [本事件回执](../validation/2026-11-20-consumer-candidate-trial-live.md)、[D35手册](environment-registry.md#停用客服候选的受限试运行（D35）) |
| 候选运行接线工件与安装 | #1604导出；#1607 `9e33886b`成套安装；#1612 `bb25c328`候选试运行 | 控制Gateway/运维入口与124独立实例1.31.0；桥/Web及Dify保持1.27.0 | 独立候选HMAC已按原安装器事务配置；为124候选Agent新增非默认模型项及批准参数，仅Agent 2改绑；目录revision3、环境/两个租户仍停用，正式路由不变 | 首轮模型名漂移及后续参数漂移失败均保留；最终正常目标登录和停用候选run完成，取得`node-124`、`matched`及`cpms_site_search`成功回执，但该账号范围内无可见站点、知识检索不可用且run无构建号；技术链通过，不等于业务语义/三功能验收；不得激活 | [本次候选试跑回执](../validation/2026-11-19-candidate-business-trial-live.md)、[运行安装回执](../validation/2026-11-19-runtime-install-live.md)、[原导出](../validation/2026-11-19-candidate-runtime-live.md) |
| 停用运行资源登记/节点能力 | #1609 `ba124707` | 控制Gateway与第二环境实例1.30.0；桥/Web/Dify保持1.27.0 | 目标created工作区/双端应用已登记，一个专属节点能力有效；环境/租户停用 | CI5420/10、本地5421/9；独立H1修复后39组与85相关通过；现场原安装器→服务身份登记/重复PID保持、节点自动HTTPS回执及11工具兼容通过；正常身份/回调/知识/合法run/业务/激活未验 | [本次回执](../validation/2026-11-19-runtime-registration-live.md)、[手册](../../ops/environments/gateway/install.md#已安装运行资源登记) |
| 正式HTTPS工具扩展 | #1556 `981b6e09`，固定制品取#1562 | 已安装既有API域名工具扩展，仅共享宿主来源到新回环实例 | 工具传输路径已接通；原公司入口/旧Gateway保留，租户未激活 | 两共享宿主匿名/错凭据401、路径/方法/HTTP及伪造来源拒绝通过；合法run工具往返与业务问答待验 | [业务连接回执](../validation/2026-11-17-business-connection-live.md)、[入口合同验证](../validation/2026-11-17-environment-tool-ingress.md) |
| 共享执行HTTPS派发 | #1564 `01f8af66`，固定上游/实际来源/单一路径 | 已安装既有HTTPS扩展，现役平台保持 | 目标来源可到平台鉴权；新节点见下一行，租户未激活 | 目标六项、另一来源伪造头拒绝及真实应用鉴权层通过；合法run仍待验 | [现场回执](../validation/2026-11-17-shared-execution-ingress-live.md)、[开发验证](../validation/2026-11-17-shared-execution-ingress.md)、[手册](../../ops/environments/gateway/execution-ingress.md) |
| 第二环境独立共享节点 | #1566 `d9072f49`、#1567 `f47d1f42` | 独立两容器与systemd已部署，固定节点jar；原共享四容器保持 | 新执行组一个在线节点；目标应用与租户未开通 | 节点Java双TLS到目标拒绝、身份/网络/原资源保持通过；真实应用目标、能力授权、合法run和并发容量待验 | [现场回执](../validation/2026-11-17-shared-environment-node-live.md)、[手册](../../deploy/node-runtime/environment.md) |
| 客服连接唯一目录 | #1569 `c4836cfb` | 控制Gateway 1.15.0已标准CD部署；目标独立Gateway未随之升级 | 运行目录模式不写旧连接副本；现役仍管理准备模式 | 隔离58项、独立44+3及CI5232/10通过；原路由/目录/六容器保持，目录投影与真实双环境业务仍未验 | [开发回执](../validation/2026-11-17-registry-consumer-connections.md)、[原部署回执](../validation/2026-11-17-registry-consumer-live.md)、[当前部署](../validation/2026-11-17-console-environment-deployed.md) |
| 共享登录选择环境 | #1572 `bfd14ad9` | 41 Gateway及36完整登录代理/页面/桥1.15.0已配套 | 多环境关闭；单环境41保持，生效目录0 | CI5240/10、独立反例修复/重放通过；正常公司登录→管理页读取→原生退出→旧会话拒绝通过；124及双环境业务未验 | [部署复验](../validation/2026-11-17-console-environment-deployed.md)、[开发验证](../validation/2026-11-17-console-environment-sessions.md) |
| 小趋/平高发布恢复与联合验收 | #1679～1682原链修复；#1684时序统计869253ce、#1685候选复用4c1d9f69、#1686停用维护窗口9d8e64fd；#1687容量起点3276303a | 41核得1.40.2+3276303a6923，95为1.40.2+9d8e64fd08e5；开发机worker已同步 | 小趋先前三业务候选与页面发布恢复、平高本轮激活恢复均通过；95最终双租户停用/路由0；优链充管家v2保留 | 平高三业务/23次容量采样/有界重叠通过；41已有三业务/追问和有限跨租户反例。登记循环待修，正常退出的授权边界待闭环；95普通入口、完整隔离及原APK未闭环 | [本轮接续回执](../validation/2026-11-23-xiaoqu-onboarding-live.md) |
| 公司来源门 | #1532 `08d203f1`；#1653删除未采用Java签发工件 | 原Python验证门保留 | 未配置、未启用 | 公司Java专属离线工件已退役，历史回执保留；现规划不以公司改码为前提，不删除可选运行安全合同 | [来源门回执](../validation/2026-11-14-z-environment-delivery-receipt.md) |
| 平高重新评估/旧95试验 | #1656管理中继；#1659受管Python1.39.1；#1661冷开通1.40.0 | 新轻量Gateway已在95真实安装运行；36/41共享入口与95本机extension回调片均已按批准挂入 | 环境/租户停用、API/site关闭、正式路由0；候选资源（工作区/知识/双端应用规格）已准备未开通 | 正常后台/C端身份、16本人单、状态/详情与正确索引range已验；共享入口各六项边界及伪造来源拒绝通过；本机片：非获准来源403/明文403/未知404/错方法405/真实节点POST抵鉴权层403（停用即无授权目标）；三业务/容量/激活仍未验，95第二真实租户待补 | [冷装与候选准备回执](../validation/2026-11-22-pinggao-cold-install-live.md)、[共享HTTPS配套](../validation/2026-11-22-pinggao-shared-https-live.md)、[身份复核](../validation/2026-11-22-pinggao-candidate-readiness.md) |

获批行为唯一正文在[环境目录Feature](../acceptance/environment-directory.feature)，
[QA](../acceptance/environment-directory-qa-plan.md)给出场景步骤，[手册](environment-registry.md)讲操作。
未来能力与依赖见[规划](../architecture/2026-11-16-manual-environment-onboarding.md)和主任务#1535/P0～P5原生子任务。
截至2026-11-24本轮只读核对，41为`1.40.2+3276303a6923`（CD45348867834成功），
95为`1.40.2+9d8e64fd08e5`，双租户停用、无pending、历史路由均retracted。
95两个升级窗口均已消耗，未因本轮控制/worker修复额外重启。
36桥/Web未随本次Python修复重建；124不在本轮变更范围，原回执保留。
95普通用户精确HTTPS入口已沿#1680合并工件安装：匿名问答401、管理404、原公司路径200；
消费者会话链可达，#1681本环境UPMS分派已合并部署，不能把入口存在当业务可用之外的宣称。
本轮[D40回执](../validation/2026-11-22-installed-business-validation-live.md)区分缺项拒绝与真实业务通过。
各行旧版本是对应验收事件的版本，不是实例当前版本。124环境与两个租户均停用；
LTA-CSMS运行工件已安装，目标工作区及双端应用已登记（原D32登记证据属于租户1）。

| 原阶段 | 已证明的范围 | 未完成的完成条件 |
|---|---|---|
| P0 | 最小模块、公司数据读面、有限HTTPS/模型工具链、主机身份；平高指定三业务并发/升级重叠通过 | 长期正式网络稳定性、完整环境身份隔离和其他共享依赖成本 |
| P1/P2 | 目录/Tab/计划/任务；隔离浏览器真实API新增/编辑/停用草稿/冲突权限通过，生产显示名保存恢复通过 | 两真实环境生效目录、实际生效停用与完整权限验收；草稿停用不替代生效停用 |
| P3 | 当前成套真实冷装/重复、冷失败保留、指定跨修订切换与恢复再中断；临时实例已清理 | 前置冷装OSError未确因、TIME_WAIT预检误拒已随1.39合并修复并本地真实TCP复验；95已升级，124保持；不声称任意故障已覆盖 |
| P4 | 六步准备、知识候选链、页面安装/登记；三业务空案例拒绝已现场观察；运行共存及单调恢复已本地验收；95真实页面冷装+七步候选准备已完成（41b09d5b全线1.40.0） | 小趋/平高发布恢复已验；登记循环、真实普通入口、完整隔离仍待闭环 |
| P5 | 各单链路时点证据 | 两环境各至少两租户的三业务、追问/原客户端、隔离及升级回退联合验收 |

多租户运行共存的代码依赖已合并；正式内容发布→目标版本与准入最后切换→失败对账/单调恢复已完成本地代码验证，
控制面配套、95升级和有限发布恢复已交付，下一主线是登记循环修复、正常退出授权复验及普通入口/完整隔离，
同时保留真实业务验证条件，不重复建设候选准备或登记。
124原身份/索引维护权限缺口保持；用户提供95平高后，已核正常有单身份及既有正确索引。
下一真实业务候选转为平高，共享来源与本机回调片均已配套；95轻量模块已页面冷装健康运行；第二租户小趋充电已完成页面驱动全链（验证→发布→恢复停用）；
平高已完成三能力候选验证、有界容量及发布恢复；下一步只补普通入口与剩余隔离，不撤回124成果。
用户已批准既有受限HTTPS入口作为124正式服务API入口，不再等待原443修复才有可选入口；
目标正式配置及完整双向业务/容量仍待验，原443继续保留排障。
旧APK连续会话仍待公司发包。
上述缺口限制正式开通与全矩阵结论，不阻止已授权的停用候选实施。
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
