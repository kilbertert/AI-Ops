# 文档导航与维护范围

更新：2026-10-28（北京时间）。本轮已交付功能进入测试验收与缺陷修复；阶段范围见[测试交接](testing-handoff.md)。
文档描述已验证的能力和证据边界，不能把“已实现”“已部署”“测试方已验收”互相替代。

## 从哪里开始

| 读者/问题 | 当前入口 |
|---|---|
| 测试同事：接口地址、鉴权、请求/响应与用例 | [功能测试接口文档](testing-api-guide.md)（可单独转发） |
| 产品、测试：交付什么、怎样反馈缺陷 | [测试交接](testing-handoff.md) |
| 系统方向、M1–M4剩余范围 | [系统策略复核](system-strategy-audit.md) |
| 工程：真实调用链与职责 | [系统架构](system-architecture.md)、[README模块索引](../README.md#三系统模块与依赖关系) |
| 运行版本、启用状态和证据 | [当前交付状态](agents/current-delivery-state.md)；该页后半为历史记录 |
| C端：知识上传、选库、多语言、实时查询 | [WF01手册](agents/consumer-wf01/README.md) |
| 管家：公司身份、两项CPMS工具与节点 | [管家手册](agents/operator-cpms/README.md) |
| 前端：C端和管家接线 | [C端交接](agents/consumer-wf01/app-handoff.md)、[管家交接](agents/frontend-operator-handoff.md) |
| 部署、升级、回退 | [Gateway](gateway.md)、[节点](../deploy/node-runtime/README.md)、[Dify](../deploy/dify-36/README.md)、[WF01迁移](agents/consumer-wf01/live-knowledge-deploy.md) |
| 本地开发与离线诊断 | [快速上手](快速上手.md)、[旧诊断内核](thin-harness.md) |
| 设计理由、术语、自动开发流程 | [ADR](adr/)、[词汇表](../GLOSSARY.md)、[AFK](afk-workflow.md) |
| 某次执行究竟验了什么 | [验收记录](validation/)、[开发进度](开发进度/)；按日期、文件名找对应记录 |

## 各类文件如何收尾

| 文档族 | 本轮处理与后续维护 |
|---|---|
| 根README、system-architecture、system-strategy-audit | 更新为当前产品入口、三条执行路径及剩余范围；修改架构时同步 |
| current-delivery-state | 顶部给定日快照；原逐条记录保留当时结论，不再把旧“未启用”当今天状态 |
| consumer-wf01、插件说明、知识导入 | 统一动态文档、选库发布、正文语言；审核副本只作旧版本兼容 |
| operator-cpms与前端交接 | 分开API已验证、公司代码已合并、正式H5交付未证实；不推断旧provider已退役 |
| gateway、ops、deploy | 明确新旧路径适用面；保留已验证的安装/回退手顺，实际操作仍按宿主政策 |
| 快速上手、thin-harness、根SOP及architecture.md | 导航区分CLI/旧诊断内核适用面，不作为WF01或Java节点的配置流程 |
| faq-api、standard-api-contract等接口参考 | 继续描述各自接口；WF01补充语义由手册及对应ADR给出，不将固定FAQ当动态知识 |
| 按日期的validation、开发进度、reviews与ADR | 保留历史事实；新证据新增文件，旧决定由后继ADR说明，不改写当时结果 |
| docs/validation.md、docs/开发进度.md | ADR-0097只读归档；本轮未修改 |
| docs/handoff-brief.md、afk-cutover及旧前端排查 | 标明历史用途或提供当前导航；不按旧票号继续派工 |
| docs/architecture/ai-ops-architecture.* | 旧诊断内核图快照，退出当前系统图位置；当前图在system-architecture.md |
| docs/知识库材料、原型及公司源码 | 输入资料/本地材料；不批量搬入Git，不把未审核的工程记录公开 |
| 旧任务工作树内system-strategy-audit.md | 未集成的2026-10-22草稿且有他人修改，保持原样；当前文档位于本目录 |

本轮对当前入口和相关手册做内容同步，对其他文档按上述用途保留；没有声称逐条复验全部历史事实。
本轮不修改运行时参考architecture.md和根SOP，因此不命中自动CD；不进行知识库导入、工具删除或新业务开发。测试结果须注明运行修订、环境和时间。
