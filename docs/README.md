# 文档导航与维护范围

更新：2026-11-04（北京时间）。阶段状态见[交付索引](agents/current-delivery-state.md)，
本次整理见[收尾记录](validation/2026-11-04-stage-closeout.md)，开放任务见[台账](reviews/2026-11-04-stage-closeout.md)。
文档描述已验证的能力和证据边界，不能把“已实现”“已部署”“测试方已验收”互相替代。

## 从哪里开始

| 读者/问题 | 当前入口 |
|---|---|
| 测试同事：接口地址、鉴权、请求/响应与用例 | [功能测试接口文档](testing-api-guide.md)（可单独转发） |
| 产品、测试：交付什么、怎样反馈缺陷 | [测试交接](testing-handoff.md) |
| 系统方向、M1–M4剩余范围 | [系统策略复核](system-strategy-audit.md) |
| 工程：真实调用链与职责 | [系统架构](system-architecture.md)、[README模块索引](../README.md#三系统模块与依赖关系) |
| 仓库目录、旧执行器清理与迁移顺序 | [现行目录规则](architecture/repository-layout.md#现行目录规则)；迁移已完成，旧调查仅解释历史 |
| 运行版本、启用状态和证据 | [当前交付状态](agents/current-delivery-state.md)；按证据区分已合并、已部署和已验收 |
| C端：知识上传、选库、多语言、实时查询 | [WF01手册](agents/consumer-wf01/README.md) |
| 管家：公司身份、两项CPMS工具与节点 | [管家手册](agents/operator-cpms/README.md) |
| 前端：C端和管家接线 | [C端交接](agents/consumer-wf01/app-handoff.md)、[管家交接](agents/frontend-operator-handoff.md) |
| 部署、升级、回退 | [Gateway](gateway.md)、[节点](../deploy/node-runtime/README.md)、[Dify](../deploy/dify-36/README.md)、[WF01迁移](agents/consumer-wf01/live-knowledge-deploy.md) |
| 本地服务与退役边界 | [快速上手](快速上手.md)、[旧执行器退役](runtime-retirement.md) |
| 设计理由、术语、自动开发流程 | [ADR](adr/)、[词汇表](../GLOSSARY.md)、[AFK](afk-workflow.md) |
| 某次执行究竟验了什么 | [验收记录](validation/)、[开发进度](开发进度/)；按日期、文件名找对应记录 |

## 各类文档的职责与归档

| 文档族 | 维护方式 |
|---|---|
| README、架构、测试交接、两端手册 | 描述现役入口与能力；状态引用交付索引，过时命令及时订正 |
| current-delivery-state | 只保留当前状态、证据与下一步；旧快照从Git历史读取，不继续无限追加 |
| acceptance、specs、QA计划 | 保存验收合同；PROGRESS/BLOCKED的当前结论须指向最新证据，旧执行记录标注时点 |
| gateway、ops、deploy及agents操作手册 | 保存可复用方法；主机与凭据从受控记录读取，不能照历史命令恢复已退役能力 |
| validation、开发进度 | 一次里程碑一个文件；新证据新增记录，不能用本次成功改写旧失败 |
| reviews | 阶段复核与任务处置；PRD和执行依赖仍以GitHub原生issue关系为准 |
| ADR、GLOSSARY | 决定与术语；新决定由后继ADR说明，不借整理改业务合同 |
| validation.md、开发进度.md | ADR-0097只读归档；不追加、不移动 |
| 旧架构图、探索稿、原型 | 保留历史用途；不当作现役架构、正式入口或已实现承诺 |
| 原始日志、主机快照、会话、工作区清理清单 | 私有归档；仓库只留经过选择和脱敏的结论与方法 |

本轮同步当前导航、发布基线、退役状态、提示词与费用接续，补入滞留PR的历史验收摘要。
不逐条重验全部历史事实，不删除原始失败记录或用户材料。
