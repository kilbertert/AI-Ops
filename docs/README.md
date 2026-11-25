# 文档导航与维护范围

本页是阅读路由与维护约定。运行状态由[交付索引](agents/current-delivery-state.md)按领域引用证据，
不得把“已合并”“已部署”“已启用”“已验收”互相替代。历史材料按问题展开，不默认全量读取。

## 从哪里开始

| 读者/问题 | 当前入口 |
|---|---|
| 测试同事：接口地址、鉴权、请求/响应与用例 | [功能测试接口文档](testing-api-guide.md)（可单独转发） |
| 产品、测试：交付什么、怎样反馈缺陷 | [测试交接](testing-handoff.md) |
| 系统方向、M1–M4剩余范围 | [系统策略复核](system-strategy-audit.md) |
| 工程：真实调用链与职责 | [系统架构](system-architecture.md)、[README职责入口](../README.md#三系统模块与依赖关系) |
| 多环境与租户管理、后续一键开通 | [新规划](architecture/2026-11-16-manual-environment-onboarding.md)、[Snail-AI研究](agents/snail-manual-onboarding-research.md)；范围与依赖以规划为准，实际交付读状态索引 |
| 仓库目录、旧执行器清理与迁移顺序 | [现行目录规则](architecture/repository-layout.md#现行目录规则)；迁移已完成，旧调查仅解释历史 |
| 运行版本、启用状态和证据 | [当前交付状态](agents/current-delivery-state.md)；按证据区分已合并、已部署和已验收 |
| C端：知识上传、选库、多语言、实时查询 | [WF01手册](agents/consumer-wf01/README.md) |
| 管家：公司身份、SOP、受控CPMS工具与节点 | [管家手册](agents/operator-cpms/README.md) |
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
| acceptance、specs、QA计划 | 每个行为只有一份获批正文；Feature承载可观察规则，QA链接判据并给步骤；提案和历史结果不冒充现行合同 |
| gateway、ops、deploy及agents操作手册 | 保存可复用方法；主机与凭据从受控记录读取，不能照历史命令恢复已退役能力 |
| validation、开发进度 | 必要验收事件一份回执；普通进度留原Issue，不再强制双记录；既有历史保留。**两目录是现役回执流，不是归档**：ADR-0097 起新记录只进 `docs/validation/<日期>-<短名>.md` 等独立文件（`test_milestone_archive_is_frozen`/`test_assistant_cancel_handoff` 强制） |
| reviews | 阶段复核与任务处置；PRD和执行依赖仍以GitHub原生issue关系为准 |
| ADR、GLOSSARY | 决定与术语；新决定由后继ADR说明，不借整理改业务合同 |
| validation.md、开发进度.md | ADR-0097只读归档；不追加、不移动 |
| [archive/](archive/README.md) | 已退役旧架构图（原 `docs/architecture.md`）、早期方案探索与离线校准数据只读归档（ADR-0340）；不当作现役架构、正式入口或已实现承诺 |
| 本机未跟踪素材 | `原型/`、`知识库材料/` 两目录在 `.gitignore` 中，是本机素材不进版本控制——克隆后不存在，别按它们评估仓库规模 |
| 原始日志、主机快照、会话、工作区清理清单 | 私有归档；仓库只留经过选择和脱敏的结论与方法 |

## 变更前与交付时

1. 在原任务列目标、约束、可观察判据及“必读来源＋理由”。先找对应领域契约；源码/schema回答实际形状，
   获批合同回答应有行为，ADR回答取舍，部署回执回答特定环境的运行事实。冲突须核实，不自动迁就任一副本。
2. 只写本次增量。复用现有Issue、Feature、QA与手册；独立多阶段工作才另建brief。
   采用Trellis按任务选上下文、OpenSpec现行/增量分离的思路，不新增第二套任务、journal或规格目录。
   新建进度、计划或回执前，先核已有任务能否承载；只有读者、用途或生命周期确实不同才另建，
   并在原Issue/PR说明区别与权威来源。仅为再放一个链接，或重述目标、状态、下一步，不另建进度文件。
3. 更新当前段落，别在顶部追加“最新补充”。新决定链接被替代ADR；旧记录保留状态与后继。
   proposed/current/superseded/archived分别表示提案、现行合同、被替代与历史；current不等于已部署。
4. PR说明涉及的文档及“已更新／核证无需变化／历史记录”理由。本任务负责人完成整合，长期内容归对应模块维护者。
   相关路径变化可运行 `uv run python tools/verification/doc_impact.py --base origin/main`；
   它复用llmdoc路径映射，只报告已映射影响和未映射路径，不证明内容过期或业务正确。
5. 普通进度与CI结果链接原Issue/PR；真实部署、独立验收、事故等必要事件在validation留一份回执。
   记录修订、环境、范围、结果和证据引用；过去失败保留，事实笔误注明订正，不能把复验写回为历史成功。
   同一事件的回执可被多个任务引用；不同部署或复验事件可以各有回执，不能为减少文件数合并不同证据范围。
6. 完成后更新本领域状态与证据指针，将提案标完成/放弃并链接接纳结果。旧日志保持只读；
   原始运行日志、会话、拓扑和账号材料只存私有artifact，公开正文仅保留获准的方法和脱敏结论。

新增ADR仍按[决定记录约定](agents/architecture-decisions.md)：只记录不能由代码/测试重建的有意义取舍；
其余在提交体写清No-ADR理由。入口只指向职责和契约；机器可枚举的模块/字段不逐项手抄。
结构检查拦断链、失效源码引用和生成漂移；内容矛盾按当前任务有界审查，不新增必需模型门禁。

## 环境目录试点与验收

- 当前获批行为：[环境目录Feature](acceptance/environment-directory.feature)；
  [QA](acceptance/environment-directory-qa-plan.md)给出执行步骤；[操作手册](agents/environment-registry.md)讲方法。
  [规划](architecture/2026-11-16-manual-environment-onboarding.md)仅定义目标与依赖，交付证据从状态索引选择。
- 文档流程验收见[Feature](acceptance/doc-first.feature)：独立新会话从AGENTS出发回答草稿是否启用、
  真实管理是否验收、回执适用哪个版本/环境、退役CLI是否可用，以及工具声明是否等于生产全启用。
  每个回答须有明确来源；不访问生产、不凭文件名排序、不从历史恢复旧行为。
- 确定性检查：`uv run pytest tests/test_readme_architecture.py tests/test_milestone_archive_is_frozen.py tests/tooling/test_doc_impact.py`。
  前者检查职责入口与导航链接，中者保留旧归档，后者用独立Git夹具验证映射/改名/未映射路径及无写入。
- 后续10个相关任务在各自PR记录新增brief/重复回执数、入口改动、检索误用与实际冲突。
  低风险独立brief和重复进度回执目标均为0；样本不足如实说明，不为凑数造任务。
  这不是已证明的节时收益；跨领域推广等试点复核后决定。依据见[ADR 0269](adr/0269-bounded-doc-first.md)。
