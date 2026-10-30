# 仓库目录规范与迁移记录

## 现行目录规则

本节约束后续开发。下方迁移记录与调查归档解释历史，不能作为新增文件的放置依据。

| 位置 | 应放内容 |
|---|---|
| `src/aiops_diagnostics/consumer/`、`operator/` | C端与管家的业务编排、规则与交付 |
| `identity/` | 公司主体、调用者范围与共享授权规则 |
| `data/`、`health/` | 共享只读数据访问与状态、健康指标/报告 |
| `knowledge/`、`conversation/` | 知识检索/媒体/开户、会话/澄清/固定FAQ |
| `publication/`、`execution/` | 内容发布生命周期、节点委派合同/运行账本 |
| `dify/`、`gateway/` | Dify协议适配、HTTP装配/配置/调度/存储/观测 |
| `common/` | 已有的跨领域HTTP、文件、配置、语言与生命周期原语；不吸收业务编排 |
| 仓库根 | 已登记的构建、项目指令与交付入口；业务代码、工具和新文档进入对应目录 |
| Python包根 | 版本初始化及三个现役命令实现；新业务模块进入上述职责包 |
| `tests/<职责>/` | 对应职责的行为测试；管家使用`tests/operations/`，避免与Python标准库`operator`冲突 |
| `tests/`根 | 现有跨服务部署、仓库规则、退役检查与共享夹具；新增业务测试进入职责目录 |
| `tools/content/`、`verification/`、`experiments/`、`inventory/` | 内容生成/迁移、验证探针、隔离实验、公司源码索引；tools根只留说明与统一测试命令 |
| `services/`、`integrations/` | 独立运行服务源码、公司侧集成补丁；新增常驻服务不得放进tools |
| `deploy/<服务>/`、`ops/`既有目录 | 成套部署资产、环境清单/代理配置和运行手册；Gateway根层交付脚本保持唯一实现 |

未写全前缀的Python目录均位于`src/aiops_diagnostics/`。JSON、HTML、YAML等运行资源跟随使用者所在包，
通过`importlib.resources`加载并验证wheel/服务bundle中的资源；测试夹具放对应测试目录或`tests/fixtures/`。

新增功能先找实际负责该业务的模块。仅为转发增加一层、建立泛化`utils/`收容业务、
把正式运行组件放入实验目录，都不满足本规范。跨包复用依赖现有接口；共享授权规则只保留一份。

### 自动约束与变更方式

`tests/test_repository_layout.py`是根层文件与一级目录的检查清单，覆盖仓库根、`src/`、Python包根、`tests/`、
`tools/`、`services/`、`integrations/`、`deploy/`、`ops/`，并禁止恢复旧`java/`入口。
它检查Git已跟踪及未忽略的新文件，允许在已有职责目录内增加文件；构建缓存不参与判断。
因此新增文件即使被README列出，只要落回根层或新增未登记的一级目录，仍会失败。

现有`test_readme_architecture.py`继续检查模块索引、文档链接和新增导入循环，
`test_runtime_retirement.py`继续检查旧模块缺失、退役导入和旧路由不可达。
这三组测试由现有CI全量pytest执行，`verify`是保护主分支的必需检查，无需人工记得另跑。

若确实需要新增一级目录或根层入口，在同一PR中说明职责、调用者和现有目录不合适的原因，
同步本节与检查清单，并按项目ADR规则判断是否需要记录决定。不要仅为消除失败扩大清单。
文件路径可以机械检查，放进已有目录的代码是否职责单一、接口是否合理、资源是否属于该包，仍须审查确认。

本地检查：`uv run pytest tests/test_repository_layout.py tests/test_readme_architecture.py tests/test_runtime_retirement.py`。

## 2026-10-29：退役验收后开始

Python Thin Harness和诊断/设备CLI已完整删除；独立aiops-admin保留。
C端与管家原H5、Dify原生预览及独立CPMS/跨主体拒绝已通过，证据见
[退役真实验收](../validation/2026-10-29-runtime-retirement-live.md)。历史故障准确率不在此结论内。

第一片将现役Java节点16个文件从`tools/experiments/m2_close/node/`整体迁至`services/execution-node/`，
同步所有现役源码/构建/实验引用；原Java文件逐字不变，宿主安装目录不变。
隔离驱动继续在tools，Gateway镜像不再包含节点源码。验证见
[第一片回执](../validation/2026-10-29-execution-node-layout.md)。

第二片将固定模型出口由`tools/verification/m2_live/model_proxy.py`迁至`services/model-proxy/`，
同步两套Compose、镜像上下文和原测试导入，源码逐字不变；新Compose必须与新路径镜像成对交付。

第三片将C端12个Python模块及语言JSON归到`src/aiops_diagnostics/consumer/`，
23个对应测试归到`tests/consumer/`；导入、打包、monkeypatch和递归静态检查一起更新。
既有节点装配循环按6条具体边记录，本片不改变该业务装配；验收见
[第三片记录](../validation/2026-10-29-consumer-package-layout.md)。

第四片将6个知识模块及9个测试归到`knowledge/`与`tests/knowledge/`；
Dify专用适配、C端知识编排和静态页面入口保留原归属。见
[第四片记录](../validation/2026-10-29-z04-knowledge-package-layout.md)。

第五片将7个身份/范围模块及11个测试归入`identity/`；通用http_auth继续服务独立桥制品。
同一授权逻辑不按端复制，详见[第五片验证](../validation/2026-10-29-z05-identity-package-layout.md)。

第六片将7个发布/内容生命周期模块及7个测试归入`publication/`。
节点执行与Dify协议适配另片处理，见[第六片验证](../validation/2026-10-29-z06-publication-package-layout.md)。

第七片将6个执行委派模块和9个测试归入`execution/`；
现有循环边只更新模块路径，见[第七片验证](../validation/2026-10-29-z07-execution-package-layout.md)。

第八片将25个Dify集成模块和模板归入`dify/`，29测试归`tests/dify/`；
独立桥代码和ExecStart成套切换，见[第八片验证](../validation/2026-10-29-z09-dify-package-layout.md)。

第九片将12个领域模块分入operator/health/data，25测试归operations/health/data，
见[第九片验证](../validation/2026-10-29-z10-domain-package-layout.md)。

2026-10-30追加：14个通用/会话模块与FAQ资源归common/conversation，17测试同迁；
见[本片验证](../validation/2026-10-30-common-conversation-layout.md)。

Gateway追加：10个网关模块归gateway，管家会话归operator，知识HTML跟随knowledge；
22测试归类，三个服务/运维命令保留实际实现。见[验证](../validation/2026-10-30-gateway-package-layout.md)。

工具追加：`tools/`根层按用途分入content、verification、experiments/m2_close、inventory；
公司Java补丁移入integrations/cpms-java，25测试归tests/tooling、9测试归tests/integrations。
见[本片验证](../validation/2026-10-30-tooling-layout.md)。

2026-10-30：上述各片均已合并并部署，源码、测试、工具和公司集成已按职责归类。
包根4文件是版本和3个实际命令实现；tests根保留跨服务部署/退役/仓库规则检查及共享夹具。
部署面保持deploy下的node-runtime、dify-36、dify-chat、kb-service与m2-live-03成套资产；
Gateway交付脚本留在deploy根作为唯一CD入口，ops保留环境清单、反向代理/独立服务资产和运行手册。
这些位置有真实启动/交付调用者，不新增一层转发；移动仓库源码不等于重写现役宿主目录。
最终独立验证与剩余业务边界见[收尾记录](../validation/2026-10-30-z20-layout-final-acceptance.md)。

## 2026-10-28调查归档（不再作为保留或执行决定）

以下是退役范围扩大前的原调查，数量与保留建议只属于当时。旧诊断消费者迁移步骤已被完整退役取代，
不得再按其恢复旧能力；以下旧路径用于解释历史问题。

# 仓库目录梳理与 Thin Harness 退役清单

核查基线：`55a284d6`，2026-10-28。本文区分本轮实际删除和后续迁移建议；目标目录尚未整体落地。
范围已确认：删除仓内无生产调用的旧代码，仍在用的部分列出迁移清单，不下线现役诊断。
当前功能与部署结论见[系统架构](../system-architecture.md)及[交付状态](../agents/current-delivery-state.md)。

## 一、问题在职责混杂，不在目录深度本身

按 `git ls-files -z` 统计跟踪文件，不含虚拟环境、构建产物和本机材料：

| 位置 | 基线规模 | 具体问题 |
|---|---|---|
| `src/aiops_diagnostics/` | 134 个文件，129 个 Python 模块全部位于包根 | Gateway、业务、Dify、旧推理与基础能力没有目录边界 |
| `gateway_api.py` | 8,500 行，静态导入 67 个本包模块 | 同一工厂装配身份、业务路由、控制台、内部回调与文档 |
| `gateway_runtime.py` | 3,867 行，静态导入 46 个本包模块 | 诊断、问答、知识、分析与异步作业执行聚在一起 |
| `tests/` | 248 个文件，其中 244 个直接位于根层 | 功能测试难以随对应模块定位，部分测试硬编码目录深度 |
| `tools/` | 123 个文件，29 个直接位于根层 | 生成器、探针、实验和现役 Java 节点源码混放 |
| `deploy/`、`ops/` | 分别 78、35 个文件 | 部署制品、代理源码、环境声明和运维步骤交错 |
| `docs/` | 479 个文件，27 个直接位于根层 | 已有分类；主要问题是当前说明与历史证据的适用范围 |

导入数量按 AST 扫描，包括函数内部延迟导入，只计不同的本包模块；它不是线上调用量。
已有 `agent_lifecycle`、`shortcut_lifecycle`、`conversation_store` 等职责可以直接沿用，
无需再造一套 controller/service/repository 基类，也不新增泛化 `utils/` 收容混杂代码。

## 二、Thin Harness 当前仍是一条可达的执行链

```text
CLI diagnose / agent-resume
设备 /v1/runs
标准 /v1/standard/diagnoses
助手的订单检测分支
Dify 旧诊断试用
  → agent_runner.run_agent_diagnosis
  → agent_engine.AgentCoordinator
  → codex_runtime.SDKCodexSession
  → diagnostic_tools.DiagnosticToolExecutor
  → sources（有界 SQL / HTTP / TDengine / Redis）
  → journal.EvidenceJournal + agent_validator.AgentResultValidator
  → 工作区 / 作业记录 / 诊断响应
```

依据：`cli.py`、`gateway_runtime.py`、`dify_trial.py` 的调用，及 `agent_runner.py`
对协调器、工具、证据日志的装配。助手入口在 `gateway_api.py` 中仍调用
`start_standard_diagnosis`；不能仅因 WF01 和 Java 已存在就认定所有旧消费者已经迁移。

### 本轮实际删除

| 删除项 | 核查结果 | 调用方处理 |
|---|---|---|
| `config.DiagApiSettings` | 已标记 deprecated 的类型别名；跟踪代码无引用 | 现役类型为 `HttpSettings` |
| `Settings.diag_api` | 生产代码无调用，仅测试使用；实际 HTTP 数据源读 `Settings.http` | 测试统一使用 `Settings.http` |
| `AgentSettings.provider_names()` | 只将 `providers` 投影为名称；仅一个测试使用 | 该测试直接核对已解析注册表，保留顺序断言 |

这里的“无调用”指仓内跟踪代码、脚本、入口和配置检索；不宣称扫描过所有外部 Python 消费者。
旧环境变量 `AIOPS_DIAG_API_*` 的回退读取、`redacted()` 的 `diag_api` 字段仍属配置/输出兼容，
有测试覆盖，未借此次删除 Python 别名改变它们。HTTP 令牌校验、凭据脱敏、查询上限均保留。

### 仍在用的迁移清单

| 模块或链路 | 当前依赖与删除障碍 | 移除前必须完成 |
|---|---|---|
| `agent_engine`、`diagnostic_tools`、`engine` | CLI、Gateway 诊断、Dify 试用；确定性引擎还作为 advisory runbook 工具 | 明确每个入口迁往何处或退役，验新链，再删除旧调用与对应专属测试 |
| `agent_runner` | 除诊断外还提供零订单答复和模型分类 | 先将仍需的问答/分类从诊断执行中分离，不能整文件删除 |
| `codex_runtime`、`codex_launcher`、`responses_adapter` | QA、分析叙述、旧诊断共享 SDK；适配器有 CLI 和部署手册入口 | 消费者迁完后核实际服务流量与配置，再移除 SDK、脚本入口和专属适配器 |
| `agent_workspace`、`journal` | QA、分析、试用、诊断与历史证据回读 | 明确存量任务终态、历史结果读取和证据保留；不可顺手删除数据目录 |
| `agent_contracts` | 生命周期、Dify 工具目录、发布/配置、QA 块结构共用 | 将现役契约归还对应模块，再删除诊断专属合同；保留旧快照的读取判据 |
| `agent_validator` | 新 `operator_cpms.validate_body` 仍导入 `EXECUTED_MUTATION` | 先迁移共享的禁用动作判据并保留测试，再移除旧诊断验证器 |
| `sources`、`query_scope`、`order_visibility` | 身份解析、管家授权、只读分析和订单可见性共用 | 拆出真实数据访问与共享权限规则；不能随诊断引擎一起删除 |
| `ops_tools`、`ops_provider`、Dify 旧工具 | 与新 CPMS 工具共存；发布快照仍可引用旧工具名 | 查发布配置、实际调用及恢复版本；迁发布记录后移除旧 provider/端点 |
| 设备入口、`gateway_client`、便携打包 | CLI 仍注册设备命令；Windows 分发退役不等于本地打包入口退役 | 单独确认设备消费者、历史 run 回读和本地打包用途，更新命令与交付合同 |
| SOP、fixture、`docs/architecture.md` | 工作区、wheel、PyInstaller 与 CD 会携带/读取这些文件 | 先解除运行时引用，再处理文件；历史验收与 ADR 不按死代码删除 |

完整退役的完成条件：旧写入/启动入口无消费者，替代路径验收通过，旧运行不再产生，
存量结果有明确读取方案；专属源码、配置、依赖、测试、脚本、部署资产和当前文档同步清除。
生产适配器或入口关闭还需实际使用观测。本轮只核仓库，不执行生产流量切换或服务退役。

## 三、建议的目标目录

保留现有 Python 分发包名和顶层标准构建入口，以职责归类，不为整齐重命名所有文件。
下面是迁移目标，不能当成当前存在的导入路径：

```text
src/aiops_diagnostics/
  gateway/       # HTTP 装配、路由、配置、网关存储和作业调度
  identity/      # 公司登录、调用者范围、订单授权、委派凭据
  consumer/      # WF01、订单/账务问答、语言、图片输入、试用发布
  operator/      # 管家 CPMS、运维聚合与说明
  knowledge/     # 检索、媒体、租户知识库配置与同步
  publication/   # Agent/快捷动作生命周期、内容与路由绑定
  conversation/  # 会话存储、上下文、FAQ/宣传选择和澄清
  dify/          # 登录、workspace、控制台发布、DSL 和执行适配
  execution/     # Java 委派合同、任务账本、节点发布、执行配置
  diagnosis/     # 尚未退役的 Python 诊断，实现迁完后删除
  model/         # 尚被 QA/分析/诊断共同使用的模型会话与解析
  cli/           # 现有三个 CLI 模块与命令装配
  common/        # 已有 HTTP、有界文件、脱敏、语言及路径原语
services/
  execution-node/ # 现役 Java Maven 工程
  model-proxy/    # 节点仍使用的模型代理
integrations/
  cpms-java/      # 公司 Java 侧集成补丁，独立于执行节点
plugins/         # Dify 插件保持原生打包布局
deploy/          # 按 gateway / node-runtime / dify / knowledge 归属部署资产
ops/             # 运行手册、环境声明；部署动作引用 deploy 的唯一实现
tools/           # 内容生成/迁移工具、验证探针和隔离实验
tests/           # 按职责镜像子目录，跨模块 HTTP/部署验证独立放 integration/
docs/            # 架构、ADR、运行手册、规格、历史证据沿用已有分类
```

`common/` 只容纳已有的通用原语，不放业务合同、数据库表、发布规则或 Gateway 编排。
`config.py` 暂时保留装配职责；无需为了移动目录拆成多个互相依赖的配置文件。

| 现位置 | 建议归属与拆分重点 |
|---|---|
| `gateway_api.py` | `gateway/app.py` 保留装配；路由按 consumer/operator/console/internal 分组，先保持依赖注入与鉴权语义 |
| `gateway_runtime.py` | 作业调度留 `gateway/`；各业务执行分别归 consumer/operator/diagnosis/conversation |
| `caller_auth`、`company_*`、`third_session_auth`、`scope_context`、`query_scope`、`order_visibility` | `identity/`；规则只保留一份，不能按 consumer/operator 复制授权逻辑 |
| `consumer_*` | `consumer/`；JSON 与使用它的语言模块同迁 |
| `operator_cpms`、`ops_analysis*` | `operator/`；旧 `ops_tools` 与新 CPMS 的专属/共用部分先分清 |
| `knowledge_*`、`kb_onboarding`、`ragflow_console` | `knowledge/`；Dify 专有协议适配仍归 `dify/` |
| `agent_lifecycle`、`shortcut_lifecycle`、`agent_content`、`agent_manifest`、`chat_route_bindings` | `publication/`；这些是现役编辑/发布能力，不因名称含 agent 而进入待退役目录 |
| `agent_runs`、`execution_manifest`、`chat_execution`、`node_publication` | `execution/`；名称相似也不能与 `agent_engine` 混为旧执行器 |
| `codex_runtime`、`codex_launcher`、`responses_adapter`、`turn_recovery` | `model/`；先拆共享会话依赖，最终是否删除由消费者决定 |
| `agent_contracts`、`agent_workspace`、`sources`、`agent_runner` | 跨职责文件，必须先拆具体使用部分，不能整文件机械归类 |
| `tools/experiments/m2_close/node/` | `services/execution-node/`；Maven 源码和自身测试一起迁，实验驱动留 tools |
| `tools/verification/m2_live/model_proxy.py` | `services/model-proxy/`；现役 compose 启动它，不是一次性探针 |
| `tools/experiments/m2_close/` 剩余内容、`tools/verification/m2_live/` 探针 | `tools/verification/` 或 `tools/experiments/`，按实际启动者区分；不按里程碑过时推定废弃 |
| `integrations/cpms-java/cloud-charging-pile-web/` | `integrations/cpms-java/`；这是公司侧补丁，不是本仓现役节点 |
| `ops/dify-*` 与 `deploy/dify-36/` | 按 Dify 组件聚合同一部署面；脚本、JS/HTML、补丁原文、配置模板一起核对引用 |

## 四、迁移顺序与每片验收

1. **本片：核实依赖、删除无消费者的旧兼容代码。** 列出保留原因与目标归属，不改变公开路由。
2. **归位现役执行节点与代理。** 同片更新 Maven 路径、compose 镜像内路径、镜像构建上下文、
   内容锁、CI、`.gitignore` 和运行手册；确认启动命令与 jar/代理位置一致。代码路径移动不等于部署目录迁移。
3. **分块形成 Python 包与对应测试目录。** 优先职责完整的 consumer/knowledge/identity，
   核对导入图；再拆 Gateway 装配和大文件。每片只做一个职责的移动，不同时改业务语义。
4. **逐个迁移旧诊断消费者。** 分别验 C端检测、CLI、设备、标准诊断、Dify 试用、QA、分析；
   对无替代的能力明确保留或另行退役。新 Java 两工具不等价于旧诊断全部证据能力。
5. **删除最后的专属链路。** 入口、模型依赖、配置、契约、脚本与部署资产按实际剩余引用清理，
   不保留空壳转发、注释掉的旧实现或 `legacy/` 永久副本。历史验收留档。

目录移动的影响面必须覆盖：Python 绝对/延迟导入、字符串 monkeypatch、`python -m` 和
`project.scripts`、相邻 JSON/HTML/YAML、wheel `force-include`、PyInstaller、Docker COPY、
compose、脚本工作目录、测试的 `Path(__file__).parents[...]`、README、`.spec` 与仓库地图生成器。
目前 README 测试用 `PACKAGE.glob("*.py")`，引入子包时必须同步改成递归识别完整模块名，
否则“通过”只代表没扫描到新目录。不要删掉该检查来让移动过关。

每片运行 ruff、相关测试、全量回归及 wheel/sdist；Java 移动还跑 Maven 验证和隔离实验。
改变 Gateway 外部行为时必须补[一次性 Gateway 验证](../../.claude/skills/verify-aiops-gateway/SKILL.md)。
这些本地检查不证明真实诊断、Dify/Java 整链、知识库活性或生产业务准确率。

本轮未把目录迁移当作已经实施的架构决定；后续按实际依赖、兼容范围和独立 PR 更新此表。
