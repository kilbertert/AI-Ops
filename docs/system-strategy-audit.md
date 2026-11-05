# 系统策略复核：从集中开发转入测试

更新：2026-11-05（北京时间）；按主线与已存验收整理，现役基线见[交付索引](agents/current-delivery-state.md)。
旧探索稿继续保留，不能以其未合并方案代替现役架构。

## 当前结论

公司App/H5是用户入口，Gateway负责身份、授权、路由和交付；Dify官方Agent负责客服工具循环，Dify编辑面保存业务SOP，
Java节点负责管家工具循环。旧Python执行器与provider退役、入口切换和目录归位均已有部署验收。
单个运行只由其选定引擎主控，不因失败自动换引擎、直读业务或回落旧FAQ。详见[架构](system-architecture.md)。

C端当前只读客服完成首轮开发并正式投入使用，进入提测与修复阶段；管家自然语言SOP链已部署，
主租户原H5/预览与第二租户恢复已有证据；完整权限/生命周期矩阵和故障准确率仍需补证。阶段边界与测试清单见[测试交接](testing-handoff.md)。

## 已落地的关键取舍

| 议题 | 当前实现与依据 |
|---|---|
| 引擎选择 | 环境/租户/入口/场景的可信绑定选择node或dify；不把C/B角色永久等同某引擎，现役路线分别记录 |
| 个人授权 | consumer仅本人；operator用公司主体/当前站点，主账号按既有租户规则；不可用租户服务key代替真人权限 |
| Dify实时取数 | 客服Agent插件凭单轮data_grant回调，授权绑定主体/订单/环境/工作流；模型不接收凭据 |
| 发布权威 | agents.published_version与不可变agent_versions，chat_route_bindings将版本绑定实际引擎；保存不切线上，试用独立，撤回保留行并拒交付 |
| 知识正文 | RAGFlow当前检索为来源；库选择随工作流发布，库内文档索引更新不需重新发布；旧审核副本仅兼容回滚 |
| 媒体与语言 | 媒体来自本轮资料与引用，不由模型造URL；App语言传入Dify及固定模板，正文校验不等于完整翻译质量证明 |
| 工具目录 | 现役cpms-node按发布清单提供订单、监测、历史模板/账务、确定性核验和站点/费用计划；旧七工具与aiops-ops已退役，不恢复共享服务身份路径 |
| 退役范围 | 旧源码/入口/依赖/专属测试已清理，目录迁移已完成；历史数据、证据与明确恢复材料保留 |

依据：[ADR-0124](adr/0124-chat-dify-route-uses-a-one-shot-data-grant.md)、
[ADR-0127](adr/0127-publish-switches-the-route-and-retract-keeps-the-row.md)、
[ADR-0232](adr/0232-consumer-agent-conversation.md)、[ADR-0152](adr/0152-knowledge-documents-are-the-live-answer-source.md)、
[ADR-0153](adr/0153-consumer-language-follows-app.md)。

## M1–M4如何看待

| 阶段 | 已证明 | 仍未完成；本次文档收尾不自动启动 |
|---|---|---|
| M1平台与执行可行性 | 真平台/Java节点、Dify、Gateway和现有供应商可执行 | 客户内网新环境独立部署与网络验收 |
| M2真实业务闭环 | C端知识/状态/账单H5，管家原H5/预览与站点清单 | 复杂故障准确率、费用证据完整性、全语言与长期稳定性 |
| M3 SOP管理与绑定 | 两引擎版本机制；客服Agent发布/恢复和管家SOP顺序实验、换版/撤回补验 | 完整生命周期/权限组合、双租户T13及新APK连续会话 |
| M4渐进迁移 | 已切现役场景、节点常驻，旧执行器/provider退役与目录归位 | 客户独立环境拓展、持续运行；不由本阶段自动扩范围 |

后续优先处理已提测范围的缺陷、权限和证据问题。业务写操作、新场景、全量迁移单列目标，
不以完成M系列所有未来工作作为本轮C端提测的前置；也不把它们写成已经完成。

下一步按[交付索引的依赖顺序](agents/current-delivery-state.md#下一阶段顺序与依赖)推进；
开放issue逐项处置见[阶段台账](reviews/2026-11-05-architecture-stage-closeout.md)。
