# Gateway schema 1真实交付与原入口回执

日期：2026-11-11 UTC。本文只摘录父会话已执行并选定的脱敏摘要；文档作者未连接生产、未读取私有原始库/报告。
来源修订`a174f537b8150cc6924a6e55f3c03f9314d6a9c9`；旧作者证据见[阶段2验证](2026-11-11-gateway-schema-authority.md)。

## 合并、CI与部署

- [PR #1466](https://github.com/kilbertert/AI-Ops/pull/1466)已合并，46个变更路径在origin/main与最终候选逐一一致。
- [最终CI 42960004939](https://github.com/kilbertert/AI-Ops/actions/runs/42960004939)成功：**4883 passed / 10 skipped**。
  作者早期4880/9等结果保留原修订与环境，不能替代最终CI或合并计数。
- Devin首轮备份父目录umask/遍历权限缺陷已修复，唯一复审通过、无开放线程。
- [标准CD 42960484430](https://github.com/kilbertert/AI-Ops/actions/runs/42960484430)的deploy与release-notes成功；
  schema-preflight、schema-migrate、console-preflight均passed。注释标签`v1.7.0`指向合并提交。
- 实际health为**1.7.0+a174f537b815**，线上schema版本**1**。父会话已快进规范main，分别删除远端/本地分支并retire实现工作树。

## 真实迁移、备份与构造

父会话以真实服务身份检查，备份保持迁移前schema 0，quick_check通过、外键错误0；
本次备份父目录0710、schema目录0700、文件0600，服务身份可遍历/读取。线上只读check/integrity通过，
28张受管表与5张额外历史表保留。schema模块摘要与最终已验候选一致。

上线前后10张稳定表——agents、agent_versions、shortcuts、shortcut_versions、chat_route_bindings、
dify_app_registry、operator_apps、dify_workspaces、consumer_endpoints、consumer_knowledge_grants——
逻辑行数与排序列值hash全部不变。运行QA与审计依生命周期自然写入，不能扩大为全库跨时点无任何合法变化。
备份身份、原行及原始摘要私有保留；检查方法见[schema手册](../agents/gateway-schema-runbook.md)。

| 现场已有库构造 | 耗时 |
|---|---|
| DifyRunStore | 0.67ms |
| AgentRunStore | 0.40ms |
| SelfServiceStore | 0.47ms |
| ChatRouteBindingStore | 0.32ms |

这4类构造共观察12条SQL，无BEGIN、DDL、INSERT/UPDATE/DELETE。本轮未在生产制造写锁；
持锁竞争的独立判据仍来自此前临时库和一致副本。只读构造快返不等于所有业务GET都不写：TTL收敛等仍保留写事务。

## 原Dify两次查询：失败与成功分别保留

正常账号/密码与本轮验证码新登录HTTP200，原页面驱动同一应用；未改正式指针、模型配置或订单业务数据。

| 实际输入范围 | 原入口结果 | 服务端与日志证据 |
|---|---|---|
| 首次普通查单 | 约32.39秒失败，OPERATOR_MODEL_OUTPUT_UNSTABLE | 两次cpms_order_detail均succeeded，0.553/0.226秒；两次review均refused，run终态failed、29.244秒，未见数据库锁 |
| 第二次明确只询问原始状态与起止时间 | 15.28秒message_end成功、无error；页面展示状态、起止时间及来源 | run11.846秒、tool0.482秒、review1.707秒；原logs查看运行关联与1.7.0版本一致 |

两次原logs查看运行均保留各自终态，后次17个选定接口响应均200。HTTP200只证明日志入口返回，
不能把首个问答failed记成pass。第二次scope更窄，是一条真实只读查询成功，**不是首次问法原样复验，不能推导100%准确率**。
原失败样本与截图私有保留，不公开QA/订单/运行标识、token或原始响应。模型输出不稳定此前已有记录；本轮未关质量门、换模型或改提示词。

## 交付范围与未验

#1462已关闭；#1448本轮初始化/构造锁故障已闭环，等待父会话最终归档；#1463历史默认值与约束归一延期。
本docs PR仅Refs #1448/#1466，不自动关闭父issue。

Mapping是独立数据库，阶段1登录代理1.6.1完整包已修连接释放，本次Gateway1.7.0未改变其实现/接口，未为版本数字重启代理。
本轮无新Java jar、无H5更改；公司H5改动此前已由用户取消。

**未完成业务验收（根因与准确率）。** 两条不同scope的查询不代表整体业务可用率、长期稳定性或完整生产发布/撤回矩阵。
本地CAS、取消、TTL/权限与副本检查继续保留各自边界；不冒充本轮生产压力、故障介质或全部历史结构验证。

## 本次文档验证与交接

本轮只新增本回执/开发进度，更新当前状态与schema手册，不改源码、版本或README。
按仓库CI文档选择规则并显式包含目录检查：660 passed / 8 skipped，文档档计数门禁通过；
8项跳过均为缺公司GitLab身份锚点的外部复核。Ruff检查/格式与4份文档链接通过，
新增只读命令在临时库实跑：当前版本通过，缺文件/相对路径拒绝且不创建库，不把失败打印成完整性通过。
证据留`dist/verification/gateway-schema-live-receipt/`；未重复工程全量。本次仅归档既有事实/方法，不新增架构决定，提交采用No-ADR说明。
操作知识自检已把部署后只读check/integrity、旧版备份权限、稳定表时点比较及原入口失败判定写入既有手册，未增加部署器。
本轮提交、push并创建一个docs PR，父会话负责合并与issue闭环；私有原始证据不上传。
